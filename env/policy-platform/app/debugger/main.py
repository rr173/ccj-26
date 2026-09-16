"""策略逐步调试服务（可独立部署，:8004）。

为维护者针对一次输入创建可暂停、可恢复的逐步调试会话：

- POST   /debug/sessions                       创建会话（固定产物版本 + 脱敏输入）
- GET    /debug/sessions                       会话列表
- GET    /debug/sessions/{id}                  会话状态（含版本状态 / 租约 / 分支）
- GET    /debug/sessions/{id}/branches/{bid}   分支详情（含每个节点的输入/结果/错误帧）
- POST   /debug/sessions/{id}/step             推进一步（同步，cmd_id 幂等，seq 防乱序）
- POST   /debug/sessions/{id}/continue         后台推进到下一断点 / 暂停 / 错误 / 结束
- POST   /debug/sessions/{id}/pause            请求在下一节点边界暂停
- PUT    /debug/sessions/{id}/breakpoints      整体替换某分支的断点
- POST   /debug/sessions/{id}/fork             从停止点分叉并修改部分输入
- GET    /debug/sessions/{id}/compare          逐节点比较两个分支，定位第一处分歧
- POST   /debug/sessions/{id}/end              结束会话
- POST   /debug/sessions/{id}/lease            续租
- POST   /debug/sessions/{id}/lease/takeover   到期后接管租约
- GET    /debug/sessions/{id}/timeline         完整时间线 + 各分支节点 I/O + 错误

所有写操作都要求持有有效租约（holder + token）；观察者可随时 GET。
"""
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from ..common import debug_service as ds, events
from ..common.config import SERVICE_NAME
from ..common.db import SessionLocal, init_db
from ..common.routers import audit_router
from ..common.schemas import (DebugBreakpointsRequest, DebugContinueRequest,
                              DebugEndRequest, DebugForkRequest,
                              DebugLeaseRequest, DebugLeaseTakeoverRequest,
                              DebugPauseRequest, DebugSessionCreate,
                              DebugStepRequest)


@asynccontextmanager
async def lifespan(app):
    init_db()
    with SessionLocal() as s:
        # 领取本进程启动序号（带心跳），并恢复心跳已死的旧进程残留 running 分支
        epoch, recovered = ds.bootstrap_epoch(s, service=SERVICE_NAME)
        events.audit(s, SERVICE_NAME, "SERVICE_STARTED", None,
                     hostname=socket.gethostname(), pid=os.getpid(),
                     debug_epoch=epoch,
                     running_branches_recovered=recovered)
        s.commit()
    yield


app = FastAPI(title="policy-debugger", lifespan=lifespan)
app.include_router(audit_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


def _call(fn, *args, **kwargs):
    """把领域层 DebugError 翻译成 FastAPI HTTP 响应；其余异常照常抛出。"""
    try:
        return fn(*args, **kwargs)
    except ds.DebugError as e:
        raise HTTPException(e.status, e.detail())


# ---------- 会话 ----------

@app.post("/debug/sessions", status_code=201)
def create_session(req: DebugSessionCreate):
    with SessionLocal() as s:
        return _call(ds.create_session, s, policy=req.policy, inputs=req.inputs,
                     actor=req.actor, version=req.version, title=req.title,
                     breakpoints=req.breakpoints, secret_keys=req.secret_keys,
                     lease_ttl_s=req.lease_ttl_s)


@app.get("/debug/sessions")
def list_sessions(policy: str | None = None, limit: int = Query(default=50, le=500)):
    with SessionLocal() as s:
        return ds.list_sessions(s, policy=policy, limit=limit)


@app.get("/debug/sessions/{session_id}")
def get_session(session_id: int):
    with SessionLocal() as s:
        return _call(ds.get_session, s, session_id)


@app.post("/debug/sessions/{session_id}/end")
def end_session(session_id: int, req: DebugEndRequest):
    with SessionLocal() as s:
        return _call(ds.end_session, s, session_id, actor=req.actor,
                     token=req.token, reason=req.reason, cmd_id=req.cmd_id)


# ---------- 租约 ----------

@app.post("/debug/sessions/{session_id}/lease")
def renew_lease(session_id: int, req: DebugLeaseRequest):
    with SessionLocal() as s:
        return _call(ds.renew_lease, s, session_id, actor=req.actor,
                     token=req.token or "", ttl_s=req.ttl_s)


@app.post("/debug/sessions/{session_id}/lease/takeover")
def takeover_lease(session_id: int, req: DebugLeaseTakeoverRequest):
    with SessionLocal() as s:
        return _call(ds.take_lease, s, session_id, actor=req.actor,
                     ttl_s=req.ttl_s)


# ---------- 推进 / 暂停 / 断点 / 分叉 ----------

@app.post("/debug/sessions/{session_id}/step")
def step(session_id: int, req: DebugStepRequest):
    with SessionLocal() as s:
        return _call(ds.step, s, session_id, actor=req.actor, token=req.token,
                     branch_id=req.branch, seq=req.seq, cmd_id=req.cmd_id)


@app.post("/debug/sessions/{session_id}/continue", status_code=202)
def continue_session(session_id: int, req: DebugContinueRequest):
    with SessionLocal() as s:
        return _call(ds.continue_session, s, session_id, actor=req.actor,
                     token=req.token, branch_id=req.branch, cmd_id=req.cmd_id)


@app.post("/debug/sessions/{session_id}/pause")
def pause(session_id: int, req: DebugPauseRequest):
    with SessionLocal() as s:
        return _call(ds.request_pause, s, session_id, actor=req.actor,
                     token=req.token, branch_id=req.branch, cmd_id=req.cmd_id)


@app.put("/debug/sessions/{session_id}/breakpoints")
def set_breakpoints(session_id: int, req: DebugBreakpointsRequest):
    with SessionLocal() as s:
        return _call(ds.set_breakpoints, s, session_id, actor=req.actor,
                     token=req.token, breakpoints=req.breakpoints,
                     branch_id=req.branch, cmd_id=req.cmd_id)


@app.post("/debug/sessions/{session_id}/fork", status_code=201)
def fork(session_id: int, req: DebugForkRequest):
    with SessionLocal() as s:
        return _call(ds.fork_branch, s, session_id, actor=req.actor,
                     token=req.token, parent_branch_id=req.parent_branch,
                     input_patch=req.input_patch, seq=req.seq, cmd_id=req.cmd_id)


# ---------- 查询 ----------

@app.get("/debug/sessions/{session_id}/branches/{branch_id}")
def get_branch(session_id: int, branch_id: str):
    with SessionLocal() as s:
        return _call(ds.get_branch, s, session_id, branch_id)


@app.get("/debug/sessions/{session_id}/compare")
def compare(session_id: int, a: str | None = None, b: str | None = None):
    with SessionLocal() as s:
        return _call(ds.compare_branches, s, session_id, a, b)


@app.get("/debug/sessions/{session_id}/timeline")
def timeline(session_id: int):
    with SessionLocal() as s:
        return _call(ds.get_timeline, s, session_id)
