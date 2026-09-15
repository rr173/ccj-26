"""运行时查询服务（可独立部署）：

- POST /query           按请求上下文选版执行，失败按安全回退规则降级
- GET  /decisions       决策日志（含输入摘要、实际使用版本、回退原因）
- GET  /cache           节点加载缓存状态（部分加载可见）
- 启动时从存储恢复全部可用产物并写 SERVICE_STARTED 审计（重启可审计）

版本选择：候选 = 未撤销且 version >= min_version，取最高；失败时逐级回退。
安全回退触发条件（仅此两类）：
  1. 节点未完全加载（如滚动升级导致 runtime 不认识新算子）
  2. 执行超时（超过请求级 deadline）
若 >= min_version 的候选全部失败且 strict_min_version=false，允许继续回退到
低于 min_version 的最近可用版本，并在响应中标记 below_min_version。
"""
import os
import socket
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ..common import dsl, events
from ..common.config import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, SERVICE_NAME
from ..common.db import SessionLocal, init_db
from ..common.models import Artifact, Decision, Policy
from ..common.routers import audit_router
from ..common.schemas import QueryRequest
from ..common.serialize import decision_dict, summarize_inputs

# 节点加载缓存：(policy, version) -> 加载结果
cache: dict = {}


def _load_artifact(art) -> dict:
    """把产物加载进执行缓存；校验所有节点算子在本 runtime 注册表中可用。

    算子缺失 => ok=False（部分节点未加载），调用方按回退规则处理。
    """
    key = (art.policy_name, art.version)
    if key in cache:
        return cache[key]
    missing = sorted({n["op"] for n in art.nodes.values()
                      if n.get("kind") == "op" and n["op"] not in dsl.OPS})
    loaded = {
        "ok": not missing,
        "missing_ops": missing,
        "nodes": art.nodes,
        "topo": art.topo,
        "entry": art.entry_node,
        "dep_chain": art.dep_chain,
        "version": art.version,
    }
    cache[key] = loaded
    return loaded


@asynccontextmanager
async def lifespan(app):
    init_db()
    warmed = 0
    with SessionLocal() as s:
        # 重启恢复：预热每个策略最新可用版本
        for art in s.query(Artifact).filter_by(revoked=False).all():
            _load_artifact(art)
            warmed += 1
        events.audit(s, SERVICE_NAME, "SERVICE_STARTED", None,
                     hostname=socket.gethostname(), pid=os.getpid(),
                     artifacts_warmed=warmed)
        s.commit()
    yield


app = FastAPI(title="policy-runtime", lifespan=lifespan)
app.include_router(audit_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


def _record_decision(s, req: QueryRequest, used_version, result, error,
                     attempts, below_min, latency_ms, dep_chain) -> int:
    d = Decision(
        request_id=req.request_id,
        policy=req.policy,
        requested_min_version=req.min_version,
        used_version=used_version,
        below_min_version=below_min,
        fallback_from=attempts[0]["version"] if attempts else None,
        fallback_reason=attempts or None,
        input_summary=summarize_inputs(req.inputs),
        result=result,
        error=error,
        latency_ms=round(latency_ms, 3),
        dep_chain=dep_chain,
    )
    s.add(d)
    s.commit()
    s.refresh(d)
    return d.id


@app.post("/query")
def query(req: QueryRequest):
    t0 = time.monotonic()
    timeout_ms = min(req.timeout_ms or DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS)
    with SessionLocal() as s:
        policy = s.query(Policy).filter_by(name=req.policy).first()
        if not policy:
            raise HTTPException(404, f"policy '{req.policy}' not found")

        base = s.query(Artifact).filter_by(policy_name=req.policy, revoked=False)
        candidates = (base.filter(Artifact.version >= req.min_version)
                      .order_by(Artifact.version.desc()).all())
        below_min = False
        if not candidates and not req.strict_min_version:
            # 安全网：没有满足最低版本要求的可用产物时，回退到低于要求的最近可用版本
            candidates = base.order_by(Artifact.version.desc()).all()
            below_min = bool(candidates)
        if not candidates:
            raise HTTPException(409, {
                "error": "no_available_version",
                "policy": req.policy,
                "min_version": req.min_version,
            })

        attempts = []
        for art in candidates:
            loaded = _load_artifact(art)
            if not loaded["ok"]:
                attempts.append({"version": art.version, "reason": "nodes_not_loaded",
                                 "missing_ops": loaded["missing_ops"]})
                continue
            try:
                result = dsl.evaluate(
                    loaded["nodes"], loaded["topo"], loaded["entry"], req.inputs,
                    time.monotonic() + timeout_ms / 1000.0, dsl.OPS)
            except dsl.PolicyTimeout:
                attempts.append({"version": art.version, "reason": "timeout",
                                 "timeout_ms": timeout_ms})
                continue
            except dsl.DSLError as e:
                # 确定性错误（缺输入、除零等）：换版本结果一样，不回退
                latency = (time.monotonic() - t0) * 1000
                did = _record_decision(s, req, art.version, None, str(e),
                                       attempts, below_min, latency,
                                       loaded["dep_chain"])
                raise HTTPException(422, {"error": str(e), "version": art.version,
                                          "decision_id": did})
            latency = (time.monotonic() - t0) * 1000
            did = _record_decision(s, req, art.version, result, None,
                                   attempts, below_min, latency,
                                   loaded["dep_chain"])
            return {
                "result": result,
                "used_version": art.version,
                "requested_min_version": req.min_version,
                "below_min_version": below_min,
                "fallback": ({"from_version": attempts[0]["version"],
                              "attempts": attempts} if attempts else None),
                "decision_id": did,
                "latency_ms": round(latency, 2),
            }

        # 所有候选版本都失败
        latency = (time.monotonic() - t0) * 1000
        did = _record_decision(s, req, None, None, "all_candidate_versions_failed",
                               attempts, below_min, latency, None)
        raise HTTPException(503, {"error": "all_candidate_versions_failed",
                                  "attempts": attempts, "decision_id": did})


@app.get("/decisions")
def list_decisions(policy: str = None, limit: int = 50):
    limit = min(max(limit, 1), 500)
    with SessionLocal() as s:
        q = s.query(Decision).order_by(Decision.id.desc())
        if policy:
            q = q.filter_by(policy=policy)
        return [decision_dict(d) for d in q.limit(limit).all()]


@app.get("/decisions/{decision_id}")
def get_decision(decision_id: int):
    with SessionLocal() as s:
        d = s.get(Decision, decision_id)
        if not d:
            raise HTTPException(404, f"decision {decision_id} not found")
        return decision_dict(d)


@app.get("/cache")
def cache_status():
    return {f"{k[0]}@{k[1]}": {"ok": v["ok"], "missing_ops": v["missing_ops"]}
            for k, v in sorted(cache.items())}
