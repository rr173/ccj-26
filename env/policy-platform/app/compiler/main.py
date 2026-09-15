"""编译服务（可独立部署）：

- POST /compile           全量编译一个策略（依赖解析 + 循环检测 + 不可变版本产物）
- POST /compile/affected  增量编译：只重编译依赖闭包包含某片段的策略
- POST /compile/batch     批量编译
- POST /artifacts/{p}/{v}/revoke  撤销版本（可审计，不删除）
- GET  /artifacts/{p}     版本列表
"""
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ..common import compile_service, events
from ..common.config import SERVICE_NAME
from ..common.db import SessionLocal, init_db
from ..common.models import Artifact, Policy
from ..common.routers import audit_router
from ..common.schemas import AffectedRequest, BatchCompileRequest, CompileRequest, RevokeRequest
from ..common.serialize import artifact_dict


@asynccontextmanager
async def lifespan(app):
    init_db()
    with SessionLocal() as s:
        events.audit(s, SERVICE_NAME, "SERVICE_STARTED", None,
                     hostname=socket.gethostname(), pid=os.getpid())
        s.commit()
    yield


app = FastAPI(title="policy-compiler", lifespan=lifespan)
app.include_router(audit_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/compile")
def compile_ep(req: CompileRequest):
    with SessionLocal() as s:
        result = compile_service.compile_policy(s, req.policy, req.actor, SERVICE_NAME)
    status = result["status"]
    if status == "not_found":
        raise HTTPException(404, result)
    if status == "failed":
        raise HTTPException(422, result)
    return result


@app.post("/compile/affected")
def compile_affected_ep(req: AffectedRequest):
    with SessionLocal() as s:
        return compile_service.compile_affected(s, req.fragment, req.actor, SERVICE_NAME)


@app.post("/compile/batch")
def compile_batch_ep(req: BatchCompileRequest):
    results = []
    with SessionLocal() as s:
        for name in req.policies:
            results.append(compile_service.compile_policy(s, name, req.actor, SERVICE_NAME))
    return {"results": results}


@app.get("/artifacts/{policy}")
def list_artifacts(policy: str):
    with SessionLocal() as s:
        if not s.query(Policy).filter_by(name=policy).first():
            raise HTTPException(404, f"policy '{policy}' not found")
        arts = (s.query(Artifact).filter_by(policy_name=policy)
                .order_by(Artifact.version.desc()).all())
        return [artifact_dict(a) for a in arts]


@app.post("/artifacts/{policy}/{version}/revoke")
def revoke_artifact(policy: str, version: int, req: RevokeRequest):
    """撤销版本：标记不可用（运行时跳过），保留完整审计轨迹，不物理删除。"""
    with SessionLocal() as s:
        art = (s.query(Artifact).filter_by(policy_name=policy, version=version).first())
        if not art:
            raise HTTPException(404, f"artifact {policy}@{version} not found")
        if art.revoked:
            return {"status": "already_revoked", "policy": policy, "version": version}
        art.revoked = True
        art.revoke_reason = req.reason
        art.revoked_by = req.actor
        from ..common.models import utcnow
        art.revoked_at = utcnow()
        events.audit(s, SERVICE_NAME, "VERSION_REVOKED", req.actor,
                     policy=policy, version=version, reason=req.reason,
                     hash=art.hash)
        s.commit()
        return {"status": "revoked", "policy": policy, "version": version,
                "reason": req.reason}
