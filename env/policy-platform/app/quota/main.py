"""资源消耗台账与周期限额服务（可独立部署，:8005）。

三个组件可用 QUOTA_ROLE 分开启动（"collector" / "gate" / "accountant" / "all"）：
  - 凭证采集：HTTP 提交（任何角色都带管理/采集 API）+ collector 角色额外扫描文件台；
  - 前置余额门禁：gate 角色处理占用/放弃事件并回收超时占用；
  - 批次核算：accountant 角色销账、重试乱序凭证、自动封账。
所有状态只在数据库，任一组件中断重启后从事件箱/批次状态接着处理。

API：
  管理    POST /quota/accounts、POST /quota/rules、POST /quota/limits
  采集    POST /quota/holds（gate 在本进程时同步返回 200 终态裁决+通行令；
          gate 分离时 202 入箱）、GET /quota/holds/{serial}/verdict、
          POST /quota/vouchers、POST /quota/aborts
  核算    POST /quota/batches/seal、POST /quota/batches/auto-seal、
          GET  /quota/batches/{account}/{date}/page
  财务    GET  /quota/adjustments、POST /quota/adjustments/{id}/decision、
          POST /quota/adjustments/manual
  解释    GET  /quota/usage/{account}、GET  /quota/trace/{serial}、
          GET  /quota/vouchers、GET  /quota/holds
  运维    POST /quota/tick/{role}（测试/演示/手动触发周期）
"""
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..common import events, quota_service as qs, quota_worker as worker
from ..common import config as cfg
from ..common.config import SERVICE_NAME
from ..common.db import SessionLocal, init_db
from ..common.routers import audit_router
from ..common.schemas import (QuotaAbortSubmit, QuotaAccountCreate,
                               QuotaAdjustmentDecision, QuotaHoldSubmit,
                               QuotaLimitSet, QuotaManualAdjustment,
                               QuotaRuleRegister, QuotaSealRequest,
                               QuotaVoucherSubmit)


@asynccontextmanager
async def lifespan(app):
    init_db()
    roles = worker.parse_roles(cfg.QUOTA_ROLE)
    with SessionLocal() as s:
        events.audit(s, "quota", "SERVICE_STARTED", None,
                     hostname=socket.gethostname(), pid=os.getpid(),
                     roles=roles)
        s.commit()
    worker.start(cfg.QUOTA_ROLE)
    yield
    worker.stop()


app = FastAPI(title="policy-quota-ledger", lifespan=lifespan)
app.include_router(audit_router)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except qs.QuotaError as e:
        raise HTTPException(e.status, e.detail())


def _gate_enabled() -> bool:
    # 动态读 config，便于运行时按 QUOTA_ROLE 切换（测试/同一库多角色部署）
    from ..common import config as cfg
    return worker.ROLE_GATE in worker.parse_roles(cfg.QUOTA_ROLE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME,
            "roles": worker.parse_roles(cfg.QUOTA_ROLE)}


# ---------- 管理：账户 / 规则 / 限额 ----------

@app.post("/quota/accounts", status_code=201)
def create_account(req: QuotaAccountCreate):
    with SessionLocal() as s:
        r = _call(qs.create_account, s, req.name, req.timezone, req.actor)
        s.commit()
        return r


@app.post("/quota/rules", status_code=201)
def register_rule(req: QuotaRuleRegister):
    with SessionLocal() as s:
        r = _call(qs.register_rule, s, req.account, req.rule_name, req.mode,
                  req.initial_limit, req.actor)
        s.commit()
        return r


@app.post("/quota/limits")
def set_limit(req: QuotaLimitSet):
    with SessionLocal() as s:
        r = _call(qs.set_limit, s, req.account, req.scope_rule, req.amount,
                  req.effective_from, req.actor, req.note)
        s.commit()
        return r


# ---------- 采集端：占用申请 / 消耗凭证 / 放弃 ----------

def _submit(req, event_type, payload):
    with SessionLocal() as s:
        r = _call(qs.submit_event,
                  s, serial=req.serial, event_type=event_type,
                  account=req.account, rule_name=req.rule_name,
                  occurred_at=req.occurred_at, payload=payload, source="http")
        s.commit()
        return r


@app.post("/quota/holds")
def submit_hold(req: QuotaHoldSubmit):
    """占用申请。

    - 本进程承担 gate 职责（默认 all / 含 gate）：采集去重 + 前置门禁在**同一个
      写事务**内完成，直接返回终态裁决（200）——通过则带唯一、可重复获取的
      `verdict.token`（通行令），拒绝则 `verdict=REJECTED` + reject_reason，
      客户端拿到裁决前不得启动后续计算。
    - gate 分离部署（QUOTA_ROLE 不含 gate）：仅入采集箱（202），客户端凭 serial
      轮询 GET /quota/holds/{serial}/verdict，直到拿到同一份终态裁决。
    """
    payload = {"amount": req.amount,
               **({"ttl_s": req.ttl_s} if req.ttl_s is not None else {})}
    with SessionLocal() as s:
        if _gate_enabled():
            r = _call(qs.submit_and_adjudicate_hold,
                      s, serial=req.serial, account=req.account,
                      rule_name=req.rule_name, occurred_at=req.occurred_at,
                      amount=req.amount, ttl_s=req.ttl_s, source="http")
            s.commit()
            return r
        r = _call(qs.submit_event,
                  s, serial=req.serial, event_type=qs.EVENT_HOLD,
                  account=req.account, rule_name=req.rule_name,
                  occurred_at=req.occurred_at, payload=payload, source="http")
        s.commit()
        return JSONResponse(status_code=202, content=r)


@app.get("/quota/holds/{serial}/verdict")
def hold_verdict(serial: str):
    """按流水号取回占用裁决：终态裁决（通行令/拒签）任何重试都返回同一份；
    gate 尚未裁决（组件分离运转）时返回 202 PENDING。"""
    with SessionLocal() as s:
        r = _call(qs.get_hold_verdict, s, serial)
        s.commit()  # 首次取通行令时可能在 quota_meta 内建签名密钥，一并落库
        if not r.get("decided"):
            return JSONResponse(status_code=202, content=r)
        return r


@app.post("/quota/vouchers", status_code=202)
def submit_voucher(req: QuotaVoucherSubmit):
    return _submit(req, qs.EVENT_VOUCHER,
                   {"amount": req.amount, "kind": req.kind})


@app.post("/quota/aborts", status_code=202)
def submit_abort(req: QuotaAbortSubmit):
    return _submit(req, qs.EVENT_ABORT, {"reason": req.reason})


# ---------- 查询：占用 / 凭证 ----------

@app.get("/quota/holds")
def list_holds(serial: str | None = None, account: str | None = None,
               status: str | None = None, limit: int = 100):
    limit = min(max(limit, 1), 500)
    with SessionLocal() as s:
        q = s.query(qs.QuotaHold)
        if serial:
            q = q.filter_by(serial=serial)
        if status:
            q = q.filter_by(status=status)
        if account:
            acc = _call(qs._get_account, s, account)
            q = q.filter_by(account_id=acc.id)
        return [qs.hold_dict(h) for h in q.order_by(qs.QuotaHold.id.desc())
                .limit(limit).all()]


@app.get("/quota/vouchers")
def list_vouchers(serial: str | None = None, account: str | None = None,
                  status: str | None = None, limit: int = 100):
    limit = min(max(limit, 1), 500)
    with SessionLocal() as s:
        q = s.query(qs.QuotaVoucher)
        if serial:
            q = q.filter_by(serial=serial)
        if status:
            q = q.filter_by(status=status)
        if account:
            acc = _call(qs._get_account, s, account)
            q = q.filter_by(account_id=acc.id)
        return [qs.voucher_dict(v) for v in q.order_by(qs.QuotaVoucher.id.desc())
                .limit(limit).all()]


# ---------- 批次核算 / 只读账页 ----------

@app.post("/quota/batches/seal")
def seal_batch(req: QuotaSealRequest):
    with SessionLocal() as s:
        r = _call(qs.seal_batch, s, req.account, req.batch_date, req.actor)
        s.commit()
        return r


@app.post("/quota/batches/auto-seal")
def auto_seal(actor: str = "accountant"):
    with SessionLocal() as s:
        r = qs.auto_seal_due(s, actor=actor)
        s.commit()
        return {"sealed": len(r), "pages": r}


@app.get("/quota/batches/{account}/{batch_date}/page")
def get_page(account: str, batch_date: str):
    with SessionLocal() as s:
        return _call(qs.get_page, s, account, batch_date)


# ---------- 财务：补账 / 冲账 ----------

@app.get("/quota/adjustments")
def list_adjustments(account: str | None = None, status: str | None = None,
                     limit: int = 100):
    limit = min(max(limit, 1), 500)
    with SessionLocal() as s:
        q = s.query(qs.QuotaAdjustment)
        if account:
            acc = _call(qs._get_account, s, account)
            q = q.filter_by(account_id=acc.id)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(qs.QuotaAdjustment.id.desc()).limit(limit).all()
        return [qs.adjustment_dict(
            a, s.query(qs.QuotaAdjustmentEntry)
            .filter_by(adjustment_id=a.id).first()) for a in rows]


@app.post("/quota/adjustments/{adjustment_id}/decision")
def decide_adjustment(adjustment_id: int, req: QuotaAdjustmentDecision):
    with SessionLocal() as s:
        r = _call(qs.decide_adjustment, s, adjustment_id, req.decision,
                  req.actor, req.amount, req.note)
        s.commit()
        return r


@app.post("/quota/adjustments/manual")
def manual_adjustment(req: QuotaManualAdjustment):
    with SessionLocal() as s:
        r = _call(qs.create_manual_adjustment, s, req.account, req.scope_rule,
                  req.batch_date, req.amount, req.serial, req.actor, req.note)
        s.commit()
        return r


# ---------- 解释 / 追踪 ----------

@app.get("/quota/usage/{account}")
def explain_usage(account: str, scope_rule: str | None = None,
                  batch_date: str | None = None):
    with SessionLocal() as s:
        return _call(qs.explain_usage, s, account, scope_rule, batch_date)


@app.get("/quota/trace/{serial}")
def trace_serial(serial: str):
    with SessionLocal() as s:
        r = qs.trace_serial(s, serial)
        if not r["found"]:
            raise HTTPException(404, {"error": "serial_not_found",
                                      "serial": serial})
        return r


@app.get("/quota/events")
def list_events(status: str | None = None, event_type: str | None = None,
                limit: int = 100):
    limit = min(max(limit, 1), 500)
    with SessionLocal() as s:
        q = s.query(qs.QuotaInboxEvent)
        if status:
            q = q.filter_by(status=status)
        if event_type:
            q = q.filter_by(event_type=event_type)
        rows = q.order_by(qs.QuotaInboxEvent.id.desc()).limit(limit).all()
        return [qs.event_dict(e) for e in rows]


@app.post("/quota/events/{event_id}/requeue")
def requeue_event(event_id: int, actor: str = "operator"):
    with SessionLocal() as s:
        r = _call(qs.requeue_event, s, event_id, actor)
        s.commit()
        return r


# ---------- 运维：手动触发一个工作周期（测试/演示/补跑） ----------

@app.post("/quota/tick/{role}")
def trigger_tick(role: str):
    if role not in worker.ALL_ROLES:
        raise HTTPException(422, {"error": "unknown_role", "role": role,
                                  "valid": list(worker.ALL_ROLES)})
    return worker.trigger_tick(role)
