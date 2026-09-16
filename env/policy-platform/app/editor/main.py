"""策略编辑服务（可独立部署）：

- 片段（可复用策略片段）与策略的 CRUD
- 片段更新后调用编译服务做增量重编译（只影响依赖闭包内的策略）
- 发布入口：转发给编译服务
- 依赖链查询：实时图 + 最新产物的固化依赖链
"""
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ..common import compiler_client, events, proposal_service, scheduler
from ..common.compiler_core import CycleError, MissingRefError, resolve_order
from ..common.config import SERVICE_NAME
from ..common.db import SessionLocal, init_db
from ..common.dsl import DSLError, canonical_hash, extract_refs, validate_expr
from ..common.models import Artifact, Fragment, Policy
from ..common.routers import audit_router
from ..common.schemas import (FragmentCreate, FragmentUpdate, PolicyCreate,
                              PublishRequest)
from ..common.serialize import artifact_dict, frag_dict, policy_dict
from .proposals import router as proposals_router


@asynccontextmanager
async def lifespan(app):
    init_db()
    with SessionLocal() as s:
        events.audit(s, SERVICE_NAME, "SERVICE_STARTED", None,
                     hostname=socket.gethostname(), pid=os.getpid())
        s.commit()
    # 提案调度器：恢复未到点预约/崩溃残留，重启后继续处理（评审状态在库里不受影响）
    scheduler.start(SERVICE_NAME)
    yield
    scheduler.stop()


app = FastAPI(title="policy-editor", lifespan=lifespan)
app.include_router(audit_router)
app.include_router(proposals_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


# ---------- 片段 ----------

@app.post("/fragments", status_code=201)
def create_fragment(req: FragmentCreate):
    try:
        validate_expr(req.body)
    except DSLError as e:
        raise HTTPException(422, str(e))
    refs = sorted(extract_refs(req.body))
    with SessionLocal() as s:
        if s.query(Fragment).filter_by(name=req.name).first():
            raise HTTPException(409, f"fragment '{req.name}' already exists")
        frag = Fragment(name=req.name, version=1, body=req.body, refs=refs,
                        content_hash=canonical_hash(req.body), updated_by=req.actor)
        s.add(frag)
        events.audit(s, SERVICE_NAME, "FRAGMENT_CREATED", req.actor,
                     name=req.name, refs=refs)
        s.commit()
        missing = [r for r in refs if r not in {f.name for f in s.query(Fragment).all()}]
        return {"fragment": frag_dict(frag), "missing_refs": missing}


@app.get("/fragments")
def list_fragments():
    with SessionLocal() as s:
        return [frag_dict(f) for f in s.query(Fragment).order_by(Fragment.name).all()]


@app.get("/fragments/{name}")
def get_fragment(name: str):
    with SessionLocal() as s:
        frag = s.query(Fragment).filter_by(name=name).first()
        if not frag:
            raise HTTPException(404, f"fragment '{name}' not found")
        return frag_dict(frag)


@app.put("/fragments/{name}")
def update_fragment(name: str, req: FragmentUpdate):
    """更新片段：版本 +1，然后触发受影响策略的增量重编译。"""
    try:
        validate_expr(req.body)
    except DSLError as e:
        raise HTTPException(422, str(e))
    refs = sorted(extract_refs(req.body))
    with SessionLocal() as s:
        frag = s.query(Fragment).filter_by(name=name).first()
        if not frag:
            raise HTTPException(404, f"fragment '{name}' not found")
        old_version = frag.version
        frag.version += 1
        frag.body = req.body
        frag.refs = refs
        frag.content_hash = canonical_hash(req.body)
        frag.updated_by = req.actor
        events.audit(s, SERVICE_NAME, "FRAGMENT_UPDATED", req.actor,
                     name=name, from_version=old_version, to_version=frag.version,
                     refs=refs)
        s.commit()
        known = {f.name for f in s.query(Fragment).all()}
        missing = [r for r in refs if r not in known]
        out = {"fragment": frag_dict(frag), "missing_refs": missing}

    # 增量重编译：只重编译依赖闭包包含该片段的策略
    try:
        resp = compiler_client.compile_affected(name, req.actor)
        out["recompile"] = resp.json() if resp.status_code == 200 else {
            "error": resp.text, "status_code": resp.status_code}
    except compiler_client.CompilerUnavailable as e:
        out["recompile"] = {"error": f"compiler unavailable: {e}"}

    # 片段在提案流程之外再次变化：固定了该片段基线的等待中提案立即标冲突（评审意见保留）
    with SessionLocal() as s:
        out["conflicted_proposals"] = proposal_service.mark_drifted(
            s, trigger={"type": "fragment_updated_out_of_band", "by": req.actor,
                        "fragment": name})
    return out


# ---------- 策略 ----------

@app.post("/policies", status_code=201)
def create_policy(req: PolicyCreate):
    with SessionLocal() as s:
        if s.query(Policy).filter_by(name=req.name).first():
            raise HTTPException(409, f"policy '{req.name}' already exists")
        if not s.query(Fragment).filter_by(name=req.entry_fragment).first():
            raise HTTPException(422, f"entry fragment '{req.entry_fragment}' not found")
        p = Policy(name=req.name, entry_fragment=req.entry_fragment,
                   description=req.description)
        s.add(p)
        events.audit(s, SERVICE_NAME, "POLICY_CREATED", req.actor,
                     name=req.name, entry_fragment=req.entry_fragment)
        s.commit()
        return policy_dict(p)


@app.get("/policies")
def list_policies():
    with SessionLocal() as s:
        return [policy_dict(p) for p in s.query(Policy).order_by(Policy.name).all()]


@app.get("/policies/{name}")
def get_policy(name: str):
    with SessionLocal() as s:
        p = s.query(Policy).filter_by(name=name).first()
        if not p:
            raise HTTPException(404, f"policy '{name}' not found")
        latest = (s.query(Artifact).filter_by(policy_name=name)
                  .order_by(Artifact.version.desc()).first())
        out = policy_dict(p)
        out["latest_artifact"] = artifact_dict(latest) if latest else None
        return out


@app.get("/policies/{name}/versions")
def list_versions(name: str):
    with SessionLocal() as s:
        if not s.query(Policy).filter_by(name=name).first():
            raise HTTPException(404, f"policy '{name}' not found")
        arts = (s.query(Artifact).filter_by(policy_name=name)
                .order_by(Artifact.version.desc()).all())
        return [artifact_dict(a) for a in arts]


@app.get("/policies/{name}/chain")
def policy_chain(name: str):
    """依赖链：实时片段图（含循环/缺失引用标注）+ 最新产物固化的依赖链。"""
    with SessionLocal() as s:
        p = s.query(Policy).filter_by(name=name).first()
        if not p:
            raise HTTPException(404, f"policy '{name}' not found")
        frags = {f.name: f for f in s.query(Fragment).all()}
        nodes, edges, missing = [], [], []
        visited = set()

        def walk(fname):
            if fname in visited:
                return
            visited.add(fname)
            f = frags.get(fname)
            if f is None:
                missing.append(fname)
                return
            nodes.append({"fragment": f.name, "version": f.version,
                          "hash": f.content_hash})
            for r in f.refs:
                edges.append({"from": fname, "to": r})
                walk(r)

        walk(p.entry_fragment)
        cycle, order = None, None
        try:
            order = resolve_order(p.entry_fragment, frags)
        except CycleError as e:
            cycle = e.cycle
        except MissingRefError:
            pass
        latest = (s.query(Artifact).filter_by(policy_name=name)
                  .order_by(Artifact.version.desc()).first())
        return {
            "policy": name,
            "entry_fragment": p.entry_fragment,
            "live_graph": {"nodes": nodes, "edges": edges, "cycle": cycle,
                           "missing_refs": missing, "eval_order": order},
            "latest_artifact": ({
                "version": latest.version, "hash": latest.hash,
                "revoked": latest.revoked, "dep_chain": latest.dep_chain,
            } if latest else None),
        }


@app.post("/policies/{name}/publish")
def publish(name: str, req: PublishRequest):
    """发布：转发编译服务。编译失败不会覆盖上一份可用产物。"""
    with SessionLocal() as s:
        if not s.query(Policy).filter_by(name=name).first():
            raise HTTPException(404, f"policy '{name}' not found")
    try:
        resp = compiler_client.compile_policy(name, req.actor)
    except compiler_client.CompilerUnavailable as e:
        raise HTTPException(503, f"compiler unavailable: {e}")
    payload = resp.json()
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, payload)
    # 直接发布移动了策略基线：等待中且固定旧基线的提案随之冲突（意见保留）
    if payload.get("status") == "published":
        with SessionLocal() as s:
            payload["conflicted_proposals"] = proposal_service.mark_drifted(
                s, trigger={"type": "direct_publish", "by": req.actor,
                            "policies": [name]})
    return payload
