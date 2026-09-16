"""quota 服务后台工作器：凭证采集 / 前置门禁 / 批次核算三个组件可分开启动。

角色（QUOTA_ROLE，逗号分隔）：
  collector   扫描 SPOOL_DIR 接收文件台凭证（HTTP 提交本身即采集，不依赖本角色）
  gate        处理占用申请/放弃事件，并周期回收超时占用（异常退出的余额归还）
  accountant  配对凭证销账、周期重试乱序凭证、自动封账（本地日已过的 OPEN 批次）

所有进度都在数据库（inbox 事件 NEW/PROCESSING/DONE/FAILED）：
- 启动时把崩溃残留 PROCESSING 退回 NEW，再立即跑一个周期（宕机期间到点的工作立即补做）；
- 每个事件认领（CAS NEW->PROCESSING）后独立事务处理，崩溃后重放幂等
  （占用 serial 唯一、销账 hold_serial 唯一 + 状态 CAS、入站事件去重）。
"""
import json
import os
import threading

from . import events, quota_service as qs
from .db import SessionLocal

ROLE_GATE = "gate"
ROLE_COLLECTOR = "collector"
ROLE_ACCOUNTANT = "accountant"
ALL_ROLES = (ROLE_COLLECTOR, ROLE_GATE, ROLE_ACCOUNTANT)

_stop = threading.Event()
_threads: dict[str, threading.Thread] = {}
_worker_seq = 0
_seq_lock = threading.Lock()


def parse_roles(spec: str | None) -> list[str]:
    spec = (spec or "all").strip().lower()
    if spec == "all":
        return list(ALL_ROLES)
    roles = [p.strip() for p in spec.split(",") if p.strip()]
    bad = [r for r in roles if r not in ALL_ROLES]
    if bad:
        raise ValueError(f"unknown quota roles: {bad}; valid: {ALL_ROLES}")
    return roles or list(ALL_ROLES)


def _interval_ms() -> int:
    try:
        return max(50, int(os.getenv("QUOTA_WORKER_INTERVAL_MS", "250")))
    except ValueError:
        return 250


def _worker_id(role: str) -> str:
    global _worker_seq
    with _seq_lock:
        _worker_seq += 1
        return f"{role}-{os.getpid()}-{_worker_seq}"


def _audit(service: str, event_type: str, actor: str, **payload):
    with SessionLocal() as s:
        events.audit(s, service, event_type, actor, **payload)
        s.commit()


# ---------------------------------------------------------------------------
# gate：占用/放弃事件 + 超时回收
# ---------------------------------------------------------------------------

def gate_tick(wid: str = "gate") -> dict:
    admitted = rejected = released = awaited = failed = 0
    with SessionLocal() as s:
        qs.reap_expired(s, worker=wid)  # 内部自行提交
    while True:
        with SessionLocal() as s:
            ev = qs.claim_next_event(s, (qs.EVENT_HOLD, qs.EVENT_ABORT), wid)
            if ev is None:
                break
            try:
                if ev.event_type == qs.EVENT_HOLD:
                    h = qs.process_hold_event(s, ev)
                    admitted += h["status"] == qs.HOLD_HELD
                    rejected += h["status"] == qs.HOLD_REJECTED
                else:
                    r = qs.process_abort_event(s, ev)
                    awaited += r == "AWAIT_HOLD"
                    released += r in (qs.HOLD_SETTLED, qs.HOLD_RELEASED)
                s.commit()
            except qs.QuotaError as e:
                s.rollback()
                # FAILED（如规则未注册）留在库中，可由 requeue_event 重新入队
                with SessionLocal() as s2:
                    row = s2.get(qs.QuotaInboxEvent, ev.id)
                    if row and row.status == "PROCESSING":
                        row.status, row.last_error = "FAILED", e.code
                        s2.commit()
                failed += 1
    return {"admitted": admitted, "rejected": rejected, "released": released,
            "awaited": awaited, "failed": failed}


# ---------------------------------------------------------------------------
# accountant：凭证销账 + 乱序重试 + 自动封账
# ---------------------------------------------------------------------------

def accountant_tick(wid: str = "accountant", auto_seal: bool = True) -> dict:
    processed = settled = suspended = failed = sealed = 0
    while True:
        with SessionLocal() as s:
            ev = qs.claim_next_event(s, (qs.EVENT_VOUCHER,), wid)
            if ev is None:
                break
            try:
                r = qs.process_voucher_event(s, ev)
                settled += r["result"] == "SETTLED"
                suspended += r["result"] == "SUSPENDED"
                processed += 1
                s.commit()
            except qs.QuotaError as e:
                s.rollback()
                with SessionLocal() as s2:
                    row = s2.get(qs.QuotaInboxEvent, ev.id)
                    if row and row.status == "PROCESSING":
                        row.status, row.last_error = "FAILED", e.code
                        s2.commit()
                failed += 1
    with SessionLocal() as s:
        sweep = qs.sweep_pending_vouchers(s)
        sealed_pages = qs.auto_seal_due(s) if auto_seal else []
        s.commit()
        sealed = len(sealed_pages)
    return {"processed": processed, "settled": settled,
            "suspended": suspended, "failed": failed,
            "sweep": sweep, "sealed": sealed}


# ---------------------------------------------------------------------------
# collector：文件台采集（崩溃安全：move 进 processing 后处理，重复文件幂等）
# ---------------------------------------------------------------------------

def _spool_dirs() -> tuple[str, str, str]:
    base = os.getenv("QUOTA_SPOOL_DIR", "./quota-spool")
    return os.path.join(base, "incoming"), os.path.join(base, "processing"), \
        os.path.join(base, "done")


def ingest_spool_file(path: str, source: str = "spool") -> dict:
    """读取一个文件台 JSON（单事件），经采集箱幂等去重后落库。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise qs.QuotaError("spool_bad_json", 422, detail=str(e))
    required = {"serial", "event_type", "account", "occurred_at"}
    missing = required - data.keys()
    if missing:
        raise qs.QuotaError("spool_missing_fields", 422, missing=sorted(missing))
    from datetime import datetime
    try:
        occurred = datetime.fromisoformat(data["occurred_at"])
    except ValueError as e:
        raise qs.QuotaError("spool_bad_datetime", 422, detail=str(e))
    with SessionLocal() as s:
        r = qs.submit_event(
            s, serial=data["serial"], event_type=data["event_type"],
            account=data["account"], rule_name=data.get("rule_name", ""),
            occurred_at=occurred,
            payload=data.get("payload", {}), source=source)
        s.commit()
        return r


def collector_tick(wid: str = "collector") -> dict:
    import shutil
    incoming, processing, done = _spool_dirs()
    for d in (incoming, processing, done):
        os.makedirs(d, exist_ok=True)
    # 恢复：上次崩溃留在 processing 的文件先移回 incoming（采集箱幂等，重放安全）
    recovered = 0
    for name in sorted(os.listdir(processing)):
        shutil.move(os.path.join(processing, name), os.path.join(incoming, name))
        recovered += 1
    ingested = duplicates = 0
    for name in sorted(os.listdir(incoming)):
        src = os.path.join(incoming, name)
        if not os.path.isfile(src):
            continue
        mid = os.path.join(processing, name)
        try:
            os.rename(src, mid)
            r = ingest_spool_file(mid)
            shutil.move(mid, os.path.join(done, name))
            ingested += not r["duplicate"]
            duplicates += r["duplicate"]
        except qs.QuotaError as e:
            # 永久坏文件（字段缺失/时区缺失…）留在 processing 并留痕，不毒化整个周期
            shutil.move(mid, os.path.join(processing, name + f".bad:{e.code}"))
            _audit("quota", "SPOOL_BAD_FILE", "collector", file=name, error=e.code)
    return {"recovered": recovered, "ingested": ingested, "duplicates": duplicates}


# ---------------------------------------------------------------------------
# 线程编排
# ---------------------------------------------------------------------------

def _run(role: str, wid: str):
    fn = {ROLE_COLLECTOR: collector_tick,
          ROLE_GATE: gate_tick,
          ROLE_ACCOUNTANT: accountant_tick}[role]
    interval = _interval_ms() / 1000.0
    while not _stop.wait(interval):
        try:
            fn(wid)
        except Exception as e:  # noqa: BLE001 - 工作循环不能因单周期异常退出
            _audit("quota", f"{role.upper()}_ERROR", wid, error=str(e))


def start(roles_spec: str | None = None, *, run_immediately: bool = True):
    """幂等启动所选角色（每个角色一个后台线程）。"""
    from .config import QUOTA_ROLE
    roles = parse_roles(roles_spec or QUOTA_ROLE)
    with SessionLocal() as s:
        for role in set(roles):
            n = qs.recover_processing(s, _worker_id(role))
            if n:
                events.audit(s, "quota", "WORKER_RECOVERED", role, count=n)
        s.commit()
    for role in roles:
        if role in _threads and _threads[role].is_alive():
            continue
        wid = _worker_id(role)
        _stop.clear()
        t = threading.Thread(target=_run, args=(role, wid), daemon=True,
                             name=f"quota-{role}")
        _threads[role] = t
        t.start()
        if run_immediately:
            {ROLE_COLLECTOR: collector_tick, ROLE_GATE: gate_tick,
             ROLE_ACCOUNTANT: accountant_tick}[role](wid)
        _audit("quota", "WORKER_STARTED", wid, role=role)


def stop(timeout: float = 2.0):
    _stop.set()
    for t in list(_threads.values()):
        t.join(timeout)
    _threads.clear()


def trigger_tick(role: str):
    """手动触发一个周期（测试/演示用）。"""
    wid = _worker_id(role)
    if role == ROLE_GATE:
        return gate_tick(wid)
    if role == ROLE_ACCOUNTANT:
        return accountant_tick(wid)
    if role == ROLE_COLLECTOR:
        return collector_tick(wid)
    raise ValueError(role)
