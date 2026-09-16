"""资源消耗台账与周期限额 —— 核心领域逻辑（与 FastAPI 解耦，便于单测）。

生命周期（同一业务流水 serial 贯穿）：
  hold（判定开始，预估量先占用余额）
    └─ voucher（判定结束的真实消耗凭证）→ settlement（按真实消耗销账，差额补退）
    └─ abort / 超时未结束             → release（占用归还到产生周期）

领域规则：

- **重投消除 / 乱序接纳**：采集箱 (serial, event_type) 唯一，重复提交返回 duplicate
  而不重复入账；凭证先于占用到达时凭证保持 PENDING，后续周期自动配对销账。
- **时区批次**：批次 = 账户时区下的发生日期；占用记在开始日，销账记在凭证发生日，
  归还永远回占用产生日（跨周期占用最终回到产生它的周期）。
- **封账只读**：封账把当时账目快照成账页（账页/账页行/结算/归还/分录只追加，
  数据库触发器拒绝改删）；封账后到达的凭证一律挂起，财务确认后另记补账/冲账分录，
  原账页绝不覆盖。
- **门禁抢占**：占用在一个写事务内「锁限额版本行 → 读累计占用/已销账 → 判定 →
  写占用」。Postgres 用 SELECT … FOR UPDATE 按作用域串行，SQLite 用 BEGIN
  IMMEDIATE 全库写锁；同一作用域并发请求总量不可能突破上限。
- **销账唯一**：settlement 对 hold_serial 唯一 + 占用行 HELD→终态 CAS，
  同一流水多次销账只有一次生效；reaper 超时回收与销账竞争时只有一方赢。
- **限额版本**：变更从选定批次（含当日）起生效，不允许把生效日选到已封账批次
  （已封账批次不重算）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from functools import wraps

from . import events
from .config import QUOTA_DEFAULT_TTL_S, QUOTA_MAX_TTL_S
from .db import immediate
from .models import (QuotaAccount, QuotaAdjustment, QuotaAdjustmentEntry,
                     QuotaBatch, QuotaHold, QuotaInboxEvent, QuotaPage,
                     QuotaPageLine, QuotaRelease, QuotaRule, QuotaSettlement,
                     QuotaVersion, QuotaVoucher, utcnow)

SHARED_SCOPE = ""          # 共享池作用域（多条规则共用一份限额）
EVENT_HOLD = "hold"
EVENT_VOUCHER = "voucher"
EVENT_ABORT = "abort"
VALID_EVENT_TYPES = {EVENT_HOLD, EVENT_VOUCHER, EVENT_ABORT}

HOLD_HELD, HOLD_SETTLED, HOLD_RELEASED, HOLD_REJECTED = (
    "HELD", "SETTLED", "RELEASED", "REJECTED")
V_PENDING, V_SETTLED, V_SUSPENDED, V_ADJUSTED = (
    "PENDING", "SETTLED", "SUSPENDED", "ADJUSTED")
ADJ_PENDING, ADJ_CONFIRMED, ADJ_REJECTED = "PENDING", "CONFIRMED", "REJECTED"
BATCH_OPEN, BATCH_SEALED = "OPEN", "SEALED"

# trace 阶段排序：采集 → 门禁占用 → 销账 → 归还 → 账页 → 调整
STAGE_ORDER = {
    "ingest": 0, "hold": 1, "settle": 2, "release": 3, "page": 4,
    "adjust_pending": 5, "adjust_posted": 6,
}


class QuotaError(Exception):
    """领域错误：code 供 API 翻译成 HTTP 状态与错误体。"""

    def __init__(self, code: str, status: int = 400, **extra):
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra

    def detail(self) -> dict:
        return {"error": self.code, **self.extra}


def _write_tx(fn):
    """函数整段跑在 BEGIN IMMEDIATE 事务里（SQLite）；Postgres 由行锁/CAS 保证。

    注意：调用方必须传入「尚未开始事务」的新会话（HTTP 请求/工作器 tick 均如此），
    immediate() 在下一条语句开启事务时生效。
    """
    @wraps(fn)
    def wrapper(session, *args, **kwargs):
        with immediate():
            return fn(session, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise QuotaError("unknown_timezone", 400, timezone=name)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise QuotaError("naive_datetime", 422,
                         hint="occurred_at 必须带时区，如 2026-09-16T10:00:00+08:00")
    return dt.astimezone(timezone.utc)


def batch_date_for(account: QuotaAccount, dt: datetime) -> str:
    """发生时刻按账户时区划到结算批次（本地日 YYYY-MM-DD）。"""
    return dt.astimezone(_tz(account.timezone)).date().isoformat()


def today_for(account: QuotaAccount, now: datetime) -> str:
    return batch_date_for(account, now)


def scope_of_rule(rule: QuotaRule) -> str:
    return SHARED_SCOPE if rule.mode == "shared" else rule.rule_name


def _is_pg(session) -> bool:
    return session.bind.dialect.name == "postgresql"


def _lock_version(session, account_id: int, scope: str, batch_date: str):
    """门禁临界区：锁定该作用域在该批次适用的限额版本行（PG 行锁 / SQLite 库写锁）。"""
    q = (session.query(QuotaVersion)
         .filter(QuotaVersion.account_id == account_id,
                 QuotaVersion.scope_rule == scope,
                 QuotaVersion.effective_from <= batch_date))
    if _is_pg(session):
        q = q.with_for_update()
    return q.order_by(QuotaVersion.effective_from.desc(),
                      QuotaVersion.version.desc()).first()


def effective_version(session, account_id: int, scope: str, batch_date: str):
    return (_lock_version(session, account_id, scope, batch_date)
            if session.info.get("_quota_writing") else
            session.query(QuotaVersion)
            .filter(QuotaVersion.account_id == account_id,
                    QuotaVersion.scope_rule == scope,
                    QuotaVersion.effective_from <= batch_date)
            .order_by(QuotaVersion.effective_from.desc(),
                      QuotaVersion.version.desc()).first())


def _get_account(session, name: str) -> QuotaAccount:
    acc = session.query(QuotaAccount).filter_by(name=name).first()
    if not acc:
        raise QuotaError("account_not_found", 404, account=name)
    return acc


def _get_batch(session, account: QuotaAccount, date_str: str,
               create: bool = False) -> QuotaBatch | None:
    b = (session.query(QuotaBatch)
         .filter_by(account_id=account.id, batch_date=date_str).first())
    if b is None and create:
        b = QuotaBatch(account_id=account.id, batch_date=date_str, status=BATCH_OPEN)
        session.add(b)
        session.flush()
    return b


def _usage(session, account_id: int, scope: str, date_str: str) -> dict:
    """某作用域在某批次的实时用量构成（不重算已封账账页，封账页请读 page）。"""
    held = (session.query(QuotaHold)
            .filter_by(account_id=account_id, scope_rule=scope,
                       batch_date=date_str, status=HOLD_HELD).all())
    settled = (session.query(QuotaSettlement)
               .filter_by(account_id=account_id, scope_rule=scope,
                          batch_date=date_str).all())
    entries = (session.query(QuotaAdjustmentEntry)
               .filter_by(account_id=account_id, scope_rule=scope,
                          batch_date=date_str).all())
    held_amt = sum(h.amount for h in held)
    settled_amt = sum(x.actual_amount for x in settled)
    adj_amt = sum(e.amount for e in entries)
    return {
        "held_amount": held_amt,
        "held_serials": [h.serial for h in held],
        "settled_amount": settled_amt,
        "settled_serials": [x.serial for x in settled],
        "adjusted_amount": adj_amt,
        "adjusted_serials": [e.serial for e in entries],
        # 可花余额 = 限额 - 未结占用 - 已销账 - 已入账调整
        "used_amount": held_amt + settled_amt + adj_amt,
    }


# ---------------------------------------------------------------------------
# 管理：账户 / 规则注册 / 限额版本
# ---------------------------------------------------------------------------

@_write_tx
def create_account(session, name: str, tzname: str = "UTC",
                   actor: str = "anonymous") -> dict:
    _tz(tzname)
    if session.query(QuotaAccount).filter_by(name=name).first():
        raise QuotaError("account_exists", 409, account=name)
    acc = QuotaAccount(name=name, timezone=tzname, created_by=actor)
    session.add(acc)
    session.flush()
    events.audit(session, "quota", "ACCOUNT_CREATED", actor,
                 account=name, timezone=tzname)
    return account_dict(acc)


@_write_tx
def register_rule(session, account: str, rule_name: str, mode: str = "dedicated",
                  initial_limit: int | None = None,
                  actor: str = "anonymous") -> dict:
    if mode not in ("shared", "dedicated"):
        raise QuotaError("invalid_rule_mode", 422, mode=mode)
    acc = _get_account(session, account)
    rule = session.query(QuotaRule).filter_by(
        account_id=acc.id, rule_name=rule_name).first()
    if rule:
        if rule.mode != mode:
            raise QuotaError("rule_mode_conflict", 409,
                             rule=rule_name, existing_mode=rule.mode)
        return rule_dict(rule)  # 幂等：重复注册原样返回
    rule = QuotaRule(account_id=acc.id, rule_name=rule_name, mode=mode,
                     created_by=actor)
    session.add(rule)
    session.flush()
    created_version = None
    scope = SHARED_SCOPE if mode == "shared" else rule_name
    existing_version = (session.query(QuotaVersion)
                        .filter_by(account_id=acc.id, scope_rule=scope).first())
    if existing_version is None and (mode == "shared" or initial_limit is not None):
        # 共享池首条规则 / 独立规则带初始限额：建立 v1（从最早批次起生效）
        amount = initial_limit if initial_limit is not None else 0
        created_version = _add_version(
            session, acc, scope, amount, "0001-01-01", actor,
            note="initial limit with rule registration")
    events.audit(session, "quota", "RULE_REGISTERED", actor,
                 account=account, rule=rule_name, mode=mode,
                 initial_limit=created_version)
    return rule_dict(rule)


def _add_version(session, acc: QuotaAccount, scope: str, amount: int,
                 effective_from: str, actor: str, note: str = "") -> dict:
    if amount < 0:
        raise QuotaError("invalid_amount", 422, hint="限额不可为负")
    prev = (session.query(QuotaVersion)
            .filter_by(account_id=acc.id, scope_rule=scope)
            .order_by(QuotaVersion.version.desc()).first())
    if prev and effective_from <= prev.effective_from:
        raise QuotaError("effective_from_order", 409,
                         scope=_scope_label(scope),
                         previous_effective_from=prev.effective_from)
    # 已经封账的批次不重新计算：生效日不得落在任何已封账批次上（含同日）。
    # 仅对「变更」（已有前序版本）检查；新作用域的首个版本无历史业务，不涉及重算。
    if prev is not None:
        sealed = (session.query(QuotaBatch)
                  .filter(QuotaBatch.account_id == acc.id,
                          QuotaBatch.status == BATCH_SEALED,
                          QuotaBatch.batch_date >= effective_from)
                  .order_by(QuotaBatch.batch_date).first())
        if sealed:
            raise QuotaError("effective_batch_sealed", 409,
                             scope=_scope_label(scope),
                             sealed_batch=sealed.batch_date)
    version = (prev.version + 1) if prev else 1
    v = QuotaVersion(account_id=acc.id, scope_rule=scope, version=version,
                     limit_amount=amount, effective_from=effective_from,
                     note=note, created_by=actor)
    session.add(v)
    session.flush()
    return version_dict(v)


@_write_tx
def set_limit(session, account: str, scope_rule: str, amount: int,
              effective_from: str | None = None, actor: str = "anonymous",
              note: str = "", now: datetime | None = None) -> dict:
    """变更限额，从选定批次起算。

    scope_rule=""：账户共享池（需至少注册过一条 shared 规则）；
    scope_rule=规则名：该规则的独立限额（规则须为 dedicated）。
    """
    now = now or utcnow()
    acc = _get_account(session, account)
    scope = scope_rule or SHARED_SCOPE
    if scope != SHARED_SCOPE:
        rule = session.query(QuotaRule).filter_by(
            account_id=acc.id, rule_name=scope).first()
        if not rule:
            raise QuotaError("rule_not_found", 404, rule=scope)
        if rule.mode == "shared":
            raise QuotaError("rule_uses_shared_pool", 409, rule=scope,
                             hint="共享限额请对 scope_rule='' 设限")
    if effective_from is None:
        effective_from = today_for(acc, now)
    v = _add_version(session, acc, scope, amount, effective_from, actor, note)
    events.audit(session, "quota", "LIMIT_SET", actor, account=account,
                 scope=_scope_label(scope), amount=amount,
                 effective_from=effective_from, version=v["version"])
    return v


# ---------------------------------------------------------------------------
# 采集端：入站事件（重投消除、乱序接纳的唯一入口）
# ---------------------------------------------------------------------------

def _validate_payload(event_type: str, payload: dict) -> dict:
    p = dict(payload or {})
    if event_type == EVENT_HOLD:
        amount = p.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise QuotaError("invalid_amount", 422, hint="hold amount 须为非负整数")
        ttl = p.get("ttl_s")
        if ttl is not None:
            if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
                raise QuotaError("invalid_ttl", 422)
            p["ttl_s"] = min(max(ttl, 1), QUOTA_MAX_TTL_S)
    elif event_type == EVENT_VOUCHER:
        amount = p.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise QuotaError("invalid_amount", 422, hint="consumption 须为非负整数")
        kind = p.get("kind", "consume")
        if kind not in ("consume", "reverse"):
            raise QuotaError("invalid_voucher_kind", 422, kind=kind)
        p["kind"] = kind
    return p


@_write_tx
def submit_event(session, *, serial: str, event_type: str, account: str,
                 rule_name: str = "", occurred_at: datetime, payload: dict | None = None,
                 source: str = "http", now: datetime | None = None) -> dict:
    """把一个入站事件收进采集箱。重复 (serial,event_type) 返回 duplicate=True。"""
    now = now or utcnow()
    if not serial or not isinstance(serial, str):
        raise QuotaError("invalid_serial", 422)
    if event_type not in VALID_EVENT_TYPES:
        raise QuotaError("invalid_event_type", 422, event_type=event_type)
    payload = _validate_payload(event_type, payload or {})
    occurred_at = as_utc(occurred_at)
    acc = _get_account(session, account)  # 账户必须先开户；乱序容忍在事件层不在主数据层

    dup = (session.query(QuotaInboxEvent)
           .filter_by(serial=serial, event_type=event_type).first())
    if dup:
        if dup.payload != payload or dup.account != account or dup.rule_name != rule_name:
            raise QuotaError("serial_conflict", 409, serial=serial,
                             event_type=event_type,
                             hint="同一流水号+事件类型重投但内容不一致")
        return {"duplicate": True, "id": dup.id, "status": dup.status}

    ev = QuotaInboxEvent(serial=serial, event_type=event_type, account=acc.name,
                         rule_name=rule_name or "", occurred_at=occurred_at,
                         payload=payload, source=source, status="NEW",
                         received_at=now)
    session.add(ev)
    session.flush()
    events.audit(session, "quota", "EVENT_RECEIVED", None, serial=serial,
                 type=event_type, account=account, rule=rule_name,
                 event_id=ev.id, source=source)
    return {"duplicate": False, "id": ev.id, "status": "NEW"}


def claim_next_event(session, event_types, worker_id: str) -> QuotaInboxEvent | None:
    """原子认领一个 NEW 事件（CAS：只有一个实例认领成功）。

    SQLite 下整段必须 BEGIN IMMEDIATE：DEFERRED 事务先读快照再
    UPDATE...WHERE status='NEW' 会因各连接持有旧快照而 lost update
    （多个实例都认领同一行）；IMMEDIATE 让写者从第一句起排队，后到者
    读到的一定是前者提交后的状态。
    """
    with immediate():
        while True:
            ev = (session.query(QuotaInboxEvent)
                  .filter(QuotaInboxEvent.status == "NEW",
                          QuotaInboxEvent.event_type.in_(tuple(event_types)))
                  .order_by(QuotaInboxEvent.id).limit(1).first())
            if ev is None:
                return None
            updated = (session.query(QuotaInboxEvent)
                       .filter(QuotaInboxEvent.id == ev.id,
                               QuotaInboxEvent.status == "NEW")
                       .update({"status": "PROCESSING", "claimed_by": worker_id,
                                "attempts": QuotaInboxEvent.attempts + 1},
                               synchronize_session=False))
            session.commit()
            if updated:
                session.refresh(ev)
                return ev
            session.rollback()  # 被别的实例抢先，重查下一条


def recover_processing(session, worker_id: str) -> int:
    """启动恢复：把崩溃残留的 PROCESSING 全部退回 NEW（业务写入幂等，重放安全）。"""
    n = (session.query(QuotaInboxEvent)
         .filter(QuotaInboxEvent.status == "PROCESSING")
         .update({"status": "NEW", "claimed_by": None}, synchronize_session=False))
    if n:
        events.audit(session, "quota", "EVENTS_RECOVERED", worker_id, count=n)
        session.commit()
    return n


def requeue_event(session, event_id: int, actor: str = "anonymous") -> dict:
    ev = session.get(QuotaInboxEvent, event_id)
    if not ev:
        raise QuotaError("event_not_found", 404, event_id=event_id)
    if ev.status != "FAILED":
        raise QuotaError("event_not_failed", 409, current_status=ev.status)
    ev.status = "NEW"
    ev.claimed_by = None
    ev.last_error = None
    events.audit(session, "quota", "EVENT_REQUEUED", actor, event_id=event_id,
                 serial=ev.serial)
    return event_dict(ev)


# ---------------------------------------------------------------------------
# 门禁（gate）：占用申请 / 放弃 / 超时回收
# ---------------------------------------------------------------------------

def _resolve_scope(session, acc: QuotaAccount, rule_name: str):
    rule = (session.query(QuotaRule)
            .filter_by(account_id=acc.id, rule_name=rule_name).first())
    if not rule:
        return None, QuotaError("rule_not_registered", 422, rule=rule_name)
    return scope_of_rule(rule), None


def process_hold_event(session, ev: QuotaInboxEvent,
                       now: datetime | None = None) -> dict:
    """处理一个占用申请事件（写临界区，整段 BEGIN IMMEDIATE）。"""
    with immediate():
        return _process_hold_event(session, ev, now)


def _process_hold_event(session, ev: QuotaInboxEvent, now) -> dict:
    now = now or utcnow()
    session.info["_quota_writing"] = True
    existing = session.query(QuotaHold).filter_by(serial=ev.serial).first()
    if existing:  # 重放（崩溃恢复后）：幂等
        ev.status, ev.processed_at = "DONE", now
        return hold_dict(existing)

    acc = _get_account(session, ev.account)
    scope, err = _resolve_scope(session, acc, ev.rule_name)
    if err:
        ev.status, ev.last_error = "FAILED", err.code
        raise err

    date_str = batch_date_for(acc, ev.occurred_at)
    batch = _get_batch(session, acc, date_str, create=True)
    amount = ev.payload["amount"]
    ttl_s = ev.payload.get("ttl_s", QUOTA_DEFAULT_TTL_S)
    started = ev.occurred_at
    expires = started + timedelta(seconds=ttl_s)

    hold = QuotaHold(serial=ev.serial, account_id=acc.id, rule_name=ev.rule_name,
                     scope_rule=scope, amount=amount, batch_date=date_str,
                     ttl_s=ttl_s, started_at=started, expires_at=expires,
                     created_by=ev.source)
    reject = None
    if batch.status == BATCH_SEALED:
        reject = "batch_sealed"
    else:
        v = _lock_version(session, acc.id, scope, date_str)
        if v is None:
            reject = "no_quota_limit"
        else:
            u = _usage(session, acc.id, scope, date_str)
            if u["used_amount"] + amount > v.limit_amount:
                reject = "quota_exceeded"
    if reject:
        hold.status = HOLD_REJECTED
        hold.reject_reason = reject
        hold.finished_at = now
        session.add(hold)
        session.flush()
        events.audit(session, "quota", "HOLD_REJECTED", "gate", serial=ev.serial,
                     account=acc.name, scope=_scope_label(scope), batch=date_str,
                     amount=amount, reason=reject)
    else:
        session.add(hold)
        session.flush()
        events.audit(session, "quota", "HOLD_ADMITTED", "gate", serial=ev.serial,
                     account=acc.name, scope=_scope_label(scope), batch=date_str,
                     amount=amount, expires_at=iso(expires))
    ev.status, ev.processed_at = "DONE", now
    return hold_dict(hold)


def _release_hold(session, hold: QuotaHold, reason: str, now: datetime,
                  worker: str = "gate") -> QuotaRelease:
    """CAS 把 HELD 占用置 RELEASED 并写归还记录（目标批次=占用产生批次）。

    调用方必须已在写事务中；归还只认 hold.batch_date —— 跨周期占用也回到
    产生它的那个周期。
    """
    updated = (session.query(QuotaHold)
               .filter(QuotaHold.id == hold.id, QuotaHold.status == HOLD_HELD)
               .update({"status": HOLD_RELEASED, "finished_at": now},
                       synchronize_session=False))
    if not updated:
        session.expire(hold)
        session.refresh(hold)
        raise QuotaError("hold_not_held", 409, serial=hold.serial,
                         current_status=hold.status)
    session.expire(hold)  # CAS 是绕过身份映射的批量 UPDATE，刷新本会话视图
    session.refresh(hold)
    rel = QuotaRelease(serial=hold.serial, hold_serial=hold.serial,
                       account_id=hold.account_id, scope_rule=hold.scope_rule,
                       amount=hold.amount, batch_date=hold.batch_date,
                       reason=reason, ts=now, reaped_by=worker)
    session.add(rel)
    session.flush()
    events.audit(session, "quota", "HOLD_RELEASED", worker, serial=hold.serial,
                 scope=_scope_label(hold.scope_rule), origin_batch=hold.batch_date,
                 amount=hold.amount, reason=reason)
    return rel


@_write_tx
def process_abort_event(session, ev: QuotaInboxEvent,
                        now: datetime | None = None) -> str:
    """处理放弃事件。占用还没到（乱序）时保持 NEW，等占用到达后的周期再处理。"""
    now = now or utcnow()
    session.info["_quota_writing"] = True
    hold = session.query(QuotaHold).filter_by(serial=ev.serial).first()
    if hold is None:
        # 乱序：放弃先到、占用还没落地。退回 NEW，由后续 gate 周期重试（幂等）。
        ev.status = "NEW"
        ev.claimed_by = None
        ev.last_error = "await_hold"
        return "AWAIT_HOLD"
    if hold.status == HOLD_HELD:
        _release_hold(session, hold, ev.payload.get("reason", "abort"), now,
                      worker="gate")
    ev.status, ev.processed_at = "DONE", now
    return hold.status


@_write_tx
def reap_expired(session, now: datetime | None = None,
                 worker: str = "gate") -> list[dict]:
    """超时回收：HELD 且 expires_at <= now 的占用一律归还到产生周期。

    封账批次里跨周期仍未结束的占用同样由这里回收（账页快照记录的是封账时点）。
    """
    now = now or utcnow()
    session.info["_quota_writing"] = True
    rows = (session.query(QuotaHold)
            .filter(QuotaHold.status == HOLD_HELD,
                    QuotaHold.expires_at <= now)
            .order_by(QuotaHold.id).all())
    out = []
    for hold in rows:
        rel = _release_hold(session, hold, "timeout", now, worker=worker)
        out.append(release_dict(rel))
    if rows:
        session.commit()
    return out


# ---------------------------------------------------------------------------
# 核算（accountant）：凭证配对销账、晚到挂起、封账、补冲账
# ---------------------------------------------------------------------------

def _create_voucher(session, acc: QuotaAccount, ev: QuotaInboxEvent,
                    scope: str) -> QuotaVoucher:
    date_str = batch_date_for(acc, ev.occurred_at)
    v = QuotaVoucher(serial=ev.serial, account_id=acc.id,
                     rule_name=ev.rule_name, scope_rule=scope,
                     amount=ev.payload["amount"], kind=ev.payload.get("kind", "consume"),
                     occurred_at=ev.occurred_at, batch_date=date_str,
                     status=V_PENDING, received_at=ev.received_at)
    session.add(v)
    session.flush()
    return v


def _suspend_late(session, acc: QuotaAccount, voucher: QuotaVoucher,
                  hold: QuotaHold | None, now: datetime, detail: dict | None = None):
    """封账后到达的凭证：占用（若还在）归还产生周期；凭证挂起并开调整单。"""
    if hold is not None and hold.status == HOLD_HELD:
        _release_hold(session, hold, "sealed_late", now, worker="accountant")
    voucher.status = V_SUSPENDED
    voucher.suspend_reason = "late_voucher"
    adj = QuotaAdjustment(
        serial=voucher.serial, account_id=acc.id, scope_rule=voucher.scope_rule,
        rule_name=voucher.rule_name, batch_date=voucher.batch_date,
        voucher_amount=voucher.amount, requested_kind=voucher.kind,
        status=ADJ_PENDING, reason="late_voucher",
        detail={"voucher_id": voucher.id, "occurred_at": iso(voucher.occurred_at),
                "hold_status_when_suspended": hold.status if hold else None,
                **(detail or {})})
    session.add(adj)
    session.flush()
    events.audit(session, "quota", "VOUCHER_SUSPENDED", "accountant",
                 serial=voucher.serial, account=acc.name,
                 scope=_scope_label(voucher.scope_rule), batch=voucher.batch_date,
                 amount=voucher.amount, adjustment_id=adj.id)
    return adj


def _settle(session, acc: QuotaAccount, voucher: QuotaVoucher,
            hold: QuotaHold | None, now: datetime) -> QuotaSettlement:
    """真实消耗销账。有 HELD 占用则 CAS 核销、差额补退；无占用（凭证先到且
    占用最终被拒/超时，或独立凭证）按 held=0 全额补记，over_limit 如实标记。"""
    # 销账唯一：同一流水（hold_serial=serial）已有结算时直接返回既有记录
    existing = session.query(QuotaSettlement).filter_by(
        hold_serial=voucher.serial).first()
    if existing is not None:
        return existing
    held_amount = 0
    if hold is not None and hold.status == HOLD_HELD:
        updated = (session.query(QuotaHold)
                   .filter(QuotaHold.id == hold.id, QuotaHold.status == HOLD_HELD)
                   .update({"status": HOLD_SETTLED, "finished_at": now},
                           synchronize_session=False))
        if updated:
            held_amount = hold.amount
            session.expire(hold)
            session.refresh(hold)
        else:
            session.expire(hold)
            session.refresh(hold)  # 与 reaper 竞争落败：占用已归还，全额补记
    elif hold is not None and hold.status == HOLD_SETTLED:
        existing = session.query(QuotaSettlement).filter_by(
            hold_serial=hold.serial).first()
        if existing is not None:
            return existing

    actual = voucher.amount
    st = QuotaSettlement(
        serial=voucher.serial, hold_serial=voucher.serial, account_id=acc.id,
        scope_rule=voucher.scope_rule, held_amount=held_amount,
        actual_amount=actual, delta_amount=actual - held_amount,
        batch_date=voucher.batch_date,
        origin_batch_date=hold.batch_date if hold else voucher.batch_date,
        over_limit=False, ts=now)
    session.add(st)
    session.flush()
    # 销账发生批次是否因此超限额（跨周期销账/超时后补记都可能）：如实标记不拦账
    v = _lock_version(session, acc.id, voucher.scope_rule, voucher.batch_date)
    if v is not None:
        u = _usage(session, acc.id, voucher.scope_rule, voucher.batch_date)
        st.over_limit = u["used_amount"] > v.limit_amount
    voucher.status = V_SETTLED
    events.audit(session, "quota", "VOUCHER_SETTLED", "accountant",
                 serial=voucher.serial, account=acc.name,
                 scope=_scope_label(voucher.scope_rule), batch=voucher.batch_date,
                 held=held_amount, actual=actual, delta=actual - held_amount,
                 origin_batch=st.origin_batch_date,
                 cross_period=st.batch_date != st.origin_batch_date,
                 over_limit=st.over_limit)
    return st


@_write_tx
def process_voucher_event(session, ev: QuotaInboxEvent,
                          now: datetime | None = None) -> dict:
    """处理一张凭证。封账批次一律挂起；未封账且占用未到则保持 PENDING 待配对。"""
    now = now or utcnow()
    session.info["_quota_writing"] = True
    acc = _get_account(session, ev.account)
    scope, err = _resolve_scope(session, acc, ev.rule_name)
    if err:
        ev.status, ev.last_error = "FAILED", err.code
        raise err

    voucher = session.query(QuotaVoucher).filter_by(serial=ev.serial).first()
    if voucher is None:
        voucher = _create_voucher(session, acc, ev, scope)
    result = _pair_and_account(session, acc, voucher, now)
    if result != "AWAIT_HOLD":
        ev.status, ev.processed_at = "DONE", now
    else:
        ev.status, ev.last_error = "DONE", "paired_later"  # 事件已接收；配对由周期扫描完成
    return {"serial": voucher.serial, "result": result,
            "voucher_status": voucher.status}


def _pair_and_account(session, acc: QuotaAccount, voucher: QuotaVoucher,
                      now: datetime) -> str:
    """把凭证推进到最终状态；返回 SETTLED / SUSPENDED / AWAIT_HOLD。"""
    # 凭证是该批次的第一笔数据时，批次行在此刻才建立（允许凭证先于一切到达）
    batch = _get_batch(session, acc, voucher.batch_date, create=True)
    hold = session.query(QuotaHold).filter_by(serial=voucher.serial).first()
    if hold is not None and hold.account_id != acc.id:
        raise QuotaError("serial_account_conflict", 409, serial=voucher.serial)

    if batch is not None and batch.status == BATCH_SEALED:
        _suspend_late(session, acc, voucher, hold, now,
                      detail={"sealed_at": iso(batch.sealed_at)})
        return "SUSPENDED"
    if hold is None:
        voucher.status = V_PENDING  # 凭证先到：挂起配对，后续 accountant 周期重试
        return "AWAIT_HOLD"
    _settle(session, acc, voucher, hold, now)
    return "SETTLED"


@_write_tx
def sweep_pending_vouchers(session, now: datetime | None = None,
                           limit: int = 200) -> dict:
    """周期重试：凭证先于占用到达（或占用到了但还没配对）的 PENDING 凭证。"""
    now = now or utcnow()
    session.info["_quota_writing"] = True
    rows = (session.query(QuotaVoucher).filter_by(status=V_PENDING)
            .order_by(QuotaVoucher.id).limit(limit).all())
    settled = suspended = awaiting = 0
    for voucher in rows:
        acc = session.get(QuotaAccount, voucher.account_id)
        r = _pair_and_account(session, acc, voucher, now)
        settled += r == "SETTLED"
        suspended += r == "SUSPENDED"
        awaiting += r == "AWAIT_HOLD"
    if rows:
        session.commit()
    return {"scanned": len(rows), "settled": settled,
            "suspended": suspended, "awaiting": awaiting}


# ---------------------------------------------------------------------------
# 封账：批次 -> 只读账页（快照固化）
# ---------------------------------------------------------------------------

@_write_tx
def seal_batch(session, account: str, batch_date: str,
               actor: str = "accountant", now: datetime | None = None) -> dict:
    now = now or utcnow()
    session.info["_quota_writing"] = True
    acc = _get_account(session, account)
    batch = _get_batch(session, acc, batch_date, create=True)
    if batch.status == BATCH_SEALED:
        page = session.query(QuotaPage).filter_by(batch_id=batch.id).first()
        if page is not None:
            lines = (session.query(QuotaPageLine).filter_by(page_id=page.id)
                     .order_by(QuotaPageLine.line_no).all())
            return {"sealed": False, "duplicate": True,
                    "page": page_dict(page, lines, session, acc, batch)}

    # 1) 未配对凭证先全部按晚到挂起（仍 HELD 的占用归还产生周期）
    pending = (session.query(QuotaVoucher)
               .filter_by(account_id=acc.id, batch_date=batch_date,
                          status=V_PENDING).all())
    for voucher in pending:
        hold = session.query(QuotaHold).filter_by(serial=voucher.serial).first()
        _suspend_late(session, acc, voucher, hold, now,
                      detail={"sealed_while_pending": True})

    # 2) 账页快照：该批次所有出现过的作用域，按作用域聚合 + 逐条明细行。
    #    snapshot 必须在首次 INSERT 时就是最终内容（账页只追加、永不 UPDATE）。
    holds = (session.query(QuotaHold)
             .filter_by(account_id=acc.id, batch_date=batch_date).all())
    settlements = (session.query(QuotaSettlement)
                   .filter_by(account_id=acc.id, batch_date=batch_date).all())
    scopes = sorted({h.scope_rule for h in holds}
                    | {x.scope_rule for x in settlements})

    snapshot, lines, line_no = [], [], 0
    for scope in scopes:
        v = effective_version(session, acc.id, scope, batch_date)
        s_holds = [h for h in holds if h.scope_rule == scope]
        s_settles = [x for x in settlements if x.scope_rule == scope]
        open_holds = [h for h in s_holds if h.status == HOLD_HELD]
        rejected = [h for h in s_holds if h.status == HOLD_REJECTED]
        for st in sorted(s_settles, key=lambda x: x.id):
            line_no += 1
            lines.append(QuotaPageLine(
                line_no=line_no, serial=st.serial, scope_rule=scope,
                line_type="settlement",
                amount=st.actual_amount,
                detail={"held": st.held_amount, "delta": st.delta_amount,
                        "origin_batch": st.origin_batch_date,
                        "cross_period": st.batch_date != st.origin_batch_date,
                        "over_limit": st.over_limit}))
        for h in sorted(open_holds, key=lambda x: x.id):
            line_no += 1
            lines.append(QuotaPageLine(
                line_no=line_no, serial=h.serial, scope_rule=scope,
                line_type="open_hold",
                amount=h.amount,
                detail={"expires_at": iso(h.expires_at), "rule": h.rule_name}))
        for h in sorted(rejected, key=lambda x: x.id):
            line_no += 1
            lines.append(QuotaPageLine(
                line_no=line_no, serial=h.serial, scope_rule=scope,
                line_type="rejected",
                amount=h.amount,
                detail={"reason": h.reject_reason, "rule": h.rule_name}))
        snapshot.append({
            "scope": _scope_label(scope),
            "limit_version": v.version if v else None,
            "limit_amount": v.limit_amount if v else None,
            "settled": sum(x.actual_amount for x in s_settles),
            "held_open": sum(h.amount for h in open_holds),
            "rejected": sum(h.amount for h in rejected),
            "serials": {
                "settled": [x.serial for x in s_settles],
                "open_holds": [h.serial for h in open_holds],
                "rejected": [h.serial for h in rejected],
            },
        })

    page = QuotaPage(batch_id=batch.id, account_id=acc.id, batch_date=batch_date,
                     snapshot={"scopes": snapshot, "seal_ts": iso(now)},
                     created_by=actor)
    session.add(page)
    session.flush()  # 拿到 page.id
    for ln in lines:
        ln.page_id = page.id
    session.add_all(lines)
    batch.status = BATCH_SEALED
    batch.sealed_at = now
    batch.sealed_by = actor
    session.flush()
    events.audit(session, "quota", "BATCH_SEALED", actor, account=account,
                 batch=batch_date, scopes=len(scopes), lines=line_no,
                 pending_suspended=len(pending))
    return {"sealed": True, "duplicate": False,
            "page": page_dict(page, lines, session, acc, batch)}


@_write_tx
def auto_seal_due(session, now: datetime | None = None,
                  actor: str = "accountant") -> list[dict]:
    """把每个账户「本地日期早于今天」的仍 OPEN 批次按日期升序封账。"""
    now = now or utcnow()
    out = []
    for acc in session.query(QuotaAccount).order_by(QuotaAccount.id).all():
        today = today_for(acc, now)
        dues = (session.query(QuotaBatch)
                .filter(QuotaBatch.account_id == acc.id,
                        QuotaBatch.status == BATCH_OPEN,
                        QuotaBatch.batch_date < today)
                .order_by(QuotaBatch.batch_date).all())
        for b in dues:
            out.append(seal_batch(session, acc.name, b.batch_date, actor, now))
    return out


# ---------------------------------------------------------------------------
# 财务：晚到凭证补账 / 冲账
# ---------------------------------------------------------------------------

@_write_tx
def decide_adjustment(session, adjustment_id: int, decision: str,
                      actor: str = "finance", amount: int | None = None,
                      note: str = "", now: datetime | None = None) -> dict:
    now = now or utcnow()
    session.info["_quota_writing"] = True
    adj = session.get(QuotaAdjustment, adjustment_id)
    if not adj:
        raise QuotaError("adjustment_not_found", 404, adjustment_id=adjustment_id)
    if adj.status != ADJ_PENDING:
        raise QuotaError("adjustment_not_pending", 409, current_status=adj.status)
    if decision not in ("confirm", "reject"):
        raise QuotaError("invalid_decision", 422, decision=decision)

    if decision == "reject":
        adj.status = ADJ_REJECTED
        adj.decided_by, adj.decided_at, adj.decision_note = actor, now, note
        voucher = session.query(QuotaVoucher).filter_by(serial=adj.serial).first()
        events.audit(session, "quota", "ADJUSTMENT_REJECTED", actor,
                     adjustment_id=adj.id, serial=adj.serial,
                     voucher_status=voucher.status if voucher else None)
        return adjustment_dict(adj, None)

    signed = amount if amount is not None else (
        adj.voucher_amount if adj.requested_kind == "consume" else -adj.voucher_amount)
    entry = _post_adjustment_entry(session, adj, signed, actor, now, note)
    return adjustment_dict(adj, entry)


def _post_adjustment_entry(session, adj: QuotaAdjustment, signed_amount: int,
                           actor: str, now: datetime, note: str,
                           manual: bool = False) -> QuotaAdjustmentEntry:
    if signed_amount == 0:
        raise QuotaError("invalid_amount", 422, hint="调整金额不能为 0")
    kind = "supplement" if signed_amount > 0 else "reversal"
    # 冲账不得使该作用域该批次的调整净额为负（不能冲掉不存在的补账）
    posted = (session.query(QuotaAdjustmentEntry)
              .filter_by(account_id=adj.account_id, scope_rule=adj.scope_rule,
                         batch_date=adj.batch_date).all())
    net = sum(e.amount for e in posted)
    if net + signed_amount < 0:
        raise QuotaError("reversal_exceeds_supplement", 409,
                         posted_adjustments=net, requested=signed_amount)
    v = effective_version(session, adj.account_id, adj.scope_rule, adj.batch_date)
    entry = QuotaAdjustmentEntry(
        adjustment_id=adj.id, serial=adj.serial, account_id=adj.account_id,
        scope_rule=adj.scope_rule, batch_date=adj.batch_date,
        amount=signed_amount, kind=kind, limit_version=v.version if v else 0,
        ts=now, confirmed_by=actor)
    session.add(entry)
    adj.status = ADJ_CONFIRMED
    adj.decided_by = actor
    adj.decided_at = now
    adj.decision_note = note
    voucher = session.query(QuotaVoucher).filter_by(serial=adj.serial).first()
    if voucher is not None and not manual:
        voucher.status = V_ADJUSTED
    session.flush()
    events.audit(session, "quota", "ADJUSTMENT_CONFIRMED", actor,
                 adjustment_id=adj.id, serial=adj.serial,
                 scope=_scope_label(adj.scope_rule), batch=adj.batch_date,
                 amount=signed_amount, kind=kind,
                 voucher_settled=voucher.status if voucher else None)
    return entry


@_write_tx
def create_manual_adjustment(session, account: str, scope_rule: str,
                             batch_date: str, amount: int, serial: str,
                             actor: str = "finance", note: str = "",
                             now: datetime | None = None) -> dict:
    """财务主动补/冲账（仍只追加分录，不碰账页）。仅允许对已封账批次操作，
    未封账批次的纠正应通过占用/凭证生命周期完成。"""
    now = now or utcnow()
    session.info["_quota_writing"] = True
    acc = _get_account(session, account)
    batch = _get_batch(session, acc, batch_date)
    if batch is None or batch.status != BATCH_SEALED:
        raise QuotaError("batch_not_sealed", 409, batch=batch_date)
    if session.query(QuotaAdjustment).filter_by(serial=serial).first():
        raise QuotaError("serial_conflict", 409, serial=serial)
    adj = QuotaAdjustment(
        serial=serial, account_id=acc.id, scope_rule=scope_rule or SHARED_SCOPE,
        batch_date=batch_date, voucher_amount=abs(amount),
        requested_kind="consume" if amount > 0 else "reverse",
        status=ADJ_PENDING, reason="manual",
        detail={"manual": True, "note": note})
    session.add(adj)
    session.flush()
    entry = _post_adjustment_entry(session, adj, amount, actor, now, note,
                                   manual=True)
    return adjustment_dict(adj, entry)


# ---------------------------------------------------------------------------
# 解释与追踪：余额构成、账页、流水全链路
# ---------------------------------------------------------------------------

def explain_usage(session, account: str, scope_rule: str | None = None,
                  batch_date: str | None = None, now: datetime | None = None) -> dict:
    """解释可花余额从哪来：限额版本、占用、已销账、已入账/待处理调整逐项列出。"""
    now = now or utcnow()
    acc = _get_account(session, account)
    date_str = batch_date or today_for(acc, now)
    batch = _get_batch(session, acc, date_str)
    scopes = _list_scopes(session, acc.id, date_str, scope_rule)
    result_scopes = []
    for scope in scopes:
        v = effective_version(session, acc.id, scope, date_str)
        u = _usage(session, acc.id, scope, date_str)
        pending = (session.query(QuotaAdjustment)
                   .filter_by(account_id=acc.id, scope_rule=scope,
                              batch_date=date_str, status=ADJ_PENDING).all())
        pending_amt = sum(a.voucher_amount if a.requested_kind == "consume"
                          else -a.voucher_amount for a in pending)
        item = {
            "scope": _scope_label(scope),
            "batch_date": date_str,
            "batch_status": batch.status if batch else BATCH_OPEN,
            "limit": ({"version": v.version, "amount": v.limit_amount,
                       "effective_from": v.effective_from} if v else None),
            "held": {"amount": u["held_amount"], "serials": u["held_serials"]},
            "settled": {"amount": u["settled_amount"],
                        "serials": u["settled_serials"]},
            "adjustments_posted": {"amount": u["adjusted_amount"],
                                   "serials": u["adjusted_serials"]},
            "adjustments_pending": {"count": len(pending), "amount": pending_amt,
                                    "serials": [a.serial for a in pending],
                                    "adjustment_ids": [a.id for a in pending]},
        }
        if v is not None and (not batch or batch.status == BATCH_OPEN):
            item["available"] = v.limit_amount - u["used_amount"]
        else:
            item["available"] = None  # 已封账：账页定格，不再有"可花余额"
        result_scopes.append(item)
    return {"account": acc.name, "timezone": acc.timezone,
            "batch_date": date_str,
            "batch_status": batch.status if batch else BATCH_OPEN,
            "scopes": result_scopes}


def _list_scopes(session, account_id: int, date_str: str,
                 only: str | None) -> list[str]:
    if only is not None:
        return [only or SHARED_SCOPE]
    found = set()
    # 当日发生过业务的作用域
    for m, col in ((QuotaHold, QuotaHold.batch_date),
                   (QuotaSettlement, QuotaSettlement.batch_date),
                   (QuotaRelease, QuotaRelease.batch_date),
                   (QuotaVoucher, QuotaVoucher.batch_date),
                   (QuotaAdjustment, QuotaAdjustment.batch_date),
                   (QuotaAdjustmentEntry, QuotaAdjustmentEntry.batch_date)):
        for (scope,) in session.query(m.scope_rule).filter(
                m.account_id == account_id, col == date_str).distinct():
            found.add(scope)
    # 设过限额版本的作用域（当日无业务也可查余额）
    for (scope,) in session.query(QuotaVersion.scope_rule).filter_by(
            account_id=account_id).distinct():
        found.add(scope)
    return sorted(found)


def get_page(session, account: str, batch_date: str) -> dict:
    acc = _get_account(session, account)
    batch = _get_batch(session, acc, batch_date)
    if batch is None:
        raise QuotaError("batch_not_found", 404, batch=batch_date)
    page = session.query(QuotaPage).filter_by(batch_id=batch.id).first()
    if page is None:
        raise QuotaError("page_not_found", 404, batch=batch_date,
                         hint="批次尚未封账")
    lines = (session.query(QuotaPageLine).filter_by(page_id=page.id)
             .order_by(QuotaPageLine.line_no).all())
    return page_dict(page, lines, session, acc, batch, with_appendix=True)


def trace_serial(session, serial: str) -> dict:
    """沿唯一流水串起全过程：采集 → 占用 → 凭证/销账 → 归还 → 账页 → 调整。

    不按入库时间排序：凭证可能先于占用到达（乱序），因此按业务阶段分组，
    组内按行 id/发生时刻排序，保证任何提交顺序下叙事顺序一致。
    """
    timeline: list[dict] = []
    hold = session.query(QuotaHold).filter_by(serial=serial).first()
    voucher = session.query(QuotaVoucher).filter_by(serial=serial).first()

    # 1) 采集端：占用/放弃按事件顺序叙事；凭证事件归到销账阶段（第 3 步）
    voucher_event = None
    seen_types: set[str] = set()
    for ev in (session.query(QuotaInboxEvent).filter_by(serial=serial)
               .order_by(QuotaInboxEvent.id).all()):
        if ev.event_type in seen_types:
            continue
        seen_types.add(ev.event_type)
        if ev.event_type == EVENT_VOUCHER:
            voucher_event = ev
            continue
        timeline.append({
            "stage": "ingest", "ts": iso(ev.occurred_at),
            "label": {EVENT_HOLD: "采集端接收占用申请（判定开始）",
                      EVENT_ABORT: "采集端接收放弃通知"}.get(
                ev.event_type, ev.event_type)
                     + ("（重投已消除）" if ev.attempts > 1 else ""),
            "event_id": ev.id, "event_type": ev.event_type,
            "occurred_at": iso(ev.occurred_at), "received_at": iso(ev.received_at),
            "status": ev.status, "payload": ev.payload, "source": ev.source,
            "attempts": ev.attempts,
        })

    # 2) 门禁占用
    if hold:
        acc = session.get(QuotaAccount, hold.account_id)
        result = ("门禁拒绝：" + hold.reject_reason if hold.status == HOLD_REJECTED
                  else {HOLD_HELD: "占用余额中", HOLD_SETTLED: "已销账",
                        HOLD_RELEASED: "已归还"}.get(hold.status, hold.status))
        timeline.append({
            "stage": "hold", "ts": iso(hold.started_at),
            "label": f"前置门禁：{result}",
            "scope": _scope_label(hold.scope_rule),
            "origin_batch": hold.batch_date, "amount": hold.amount,
            "status": hold.status, "expires_at": iso(hold.expires_at),
            "account": acc.name,
        })

    # 3) 凭证 + 销账（凭证采集并入此阶段叙事）
    st = session.query(QuotaSettlement).filter_by(serial=serial).first()
    if voucher_event is not None:
        timeline.append({
            "stage": "settle", "ts": iso(voucher_event.occurred_at),
            "label": "采集端接收消耗凭证（判定结束，真实消耗）"
                     + ("（重投已消除）" if voucher_event.attempts > 1 else ""),
            "event_id": voucher_event.id,
            "amount": voucher_event.payload.get("amount"),
            "kind": voucher_event.payload.get("kind", "consume"),
            "occurred_at": iso(voucher_event.occurred_at),
            "received_at": iso(voucher_event.received_at),
            "status": voucher_event.status,
        })
    if st:
        timeline.append({
            "stage": "settle", "ts": iso(st.ts),
            "label": "按真实消耗销账（差额自动补退）",
            "voucher_amount": voucher.amount if voucher else None,
            "voucher_kind": voucher.kind if voucher else None,
            "voucher_occurred_at": iso(voucher.occurred_at) if voucher else None,
            "voucher_status": voucher.status if voucher else None,
            "batch": st.batch_date, "origin_batch": st.origin_batch_date,
            "held": st.held_amount, "actual": st.actual_amount,
            "delta": st.delta_amount,
            "cross_period": st.batch_date != st.origin_batch_date,
            "over_limit": st.over_limit,
        })

    # 4) 归还（回到产生周期）
    for rel in (session.query(QuotaRelease).filter_by(serial=serial)
                .order_by(QuotaRelease.id).all()):
        timeline.append({
            "stage": "release", "ts": iso(rel.ts),
            "label": "占用归还（回到产生周期）",
            "returned_to_batch": rel.batch_date, "amount": rel.amount,
            "reason": rel.reason, "by": rel.reaped_by,
        })

    # 5) 账页（封账固化）
    for line in (session.query(QuotaPageLine).filter_by(serial=serial)
                 .order_by(QuotaPageLine.id).all()):
        page = session.get(QuotaPage, line.page_id)
        timeline.append({
            "stage": "page", "ts": iso(page.created_at),
            "label": f"封账入只读账页（{line.line_type}）",
            "page_id": page.id, "batch": page.batch_date,
            "line_no": line.line_no, "amount": line.amount, "detail": line.detail,
        })

    # 6) 调整：挂起 -> 财务确认补/冲账（原账页不变）
    for adj in (session.query(QuotaAdjustment).filter_by(serial=serial)
                .order_by(QuotaAdjustment.id).all()):
        timeline.append({
            "stage": "adjust_pending", "ts": iso(adj.created_at),
            "label": "晚到凭证挂起，等待财务确认",
            "adjustment_id": adj.id, "batch": adj.batch_date,
            "voucher_amount": adj.voucher_amount, "reason": adj.reason,
            "status": adj.status,
        })
        entry = (session.query(QuotaAdjustmentEntry)
                 .filter_by(adjustment_id=adj.id).first())
        if entry:
            timeline.append({
                "stage": "adjust_posted", "ts": iso(entry.ts),
                "label": ("补账分录" if entry.amount > 0 else "冲账分录")
                         + "（原账页不变）",
                "entry_id": entry.id, "batch": entry.batch_date,
                "amount": entry.amount, "kind": entry.kind,
                "limit_version": entry.limit_version, "by": entry.confirmed_by,
            })

    # 业务阶段固定顺序；同阶段按时间/行序（追加时已有序，稳定排序即可）
    timeline.sort(key=lambda x: (STAGE_ORDER.get(x["stage"], 9), x["ts"] or ""))
    return {"serial": serial,
            "found": bool(timeline),
            "summary": _trace_summary(hold, voucher, st),
            "timeline": timeline}


def _trace_summary(hold, voucher, settlement) -> dict:
    return {
        "hold": hold.status if hold else None,
        "voucher": voucher.status if voucher else None,
        "settled": settlement.actual_amount if settlement else None,
        "origin_batch": hold.batch_date if hold else None,
        "voucher_batch": voucher.batch_date if voucher else None,
    }


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def _scope_label(scope: str) -> str:
    return "__shared__" if scope == SHARED_SCOPE else scope


def account_dict(a: QuotaAccount) -> dict:
    return {"id": a.id, "name": a.name, "timezone": a.timezone,
            "created_at": iso(a.created_at), "created_by": a.created_by}


def rule_dict(r: QuotaRule) -> dict:
    return {"id": r.id, "account_id": r.account_id, "rule_name": r.rule_name,
            "mode": r.mode, "scope": _scope_label(scope_of_rule(r)),
            "created_at": iso(r.created_at)}


def version_dict(v: QuotaVersion) -> dict:
    return {"id": v.id, "account_id": v.account_id,
            "scope": _scope_label(v.scope_rule), "version": v.version,
            "limit_amount": v.limit_amount, "effective_from": v.effective_from,
            "note": v.note}


def event_dict(e: QuotaInboxEvent) -> dict:
    return {"id": e.id, "serial": e.serial, "event_type": e.event_type,
            "account": e.account, "rule_name": e.rule_name,
            "occurred_at": iso(e.occurred_at), "payload": e.payload,
            "source": e.source, "status": e.status, "claimed_by": e.claimed_by,
            "attempts": e.attempts, "last_error": e.last_error,
            "received_at": iso(e.received_at), "processed_at": iso(e.processed_at)}


def hold_dict(h: QuotaHold) -> dict:
    return {"id": h.id, "serial": h.serial, "account_id": h.account_id,
            "rule_name": h.rule_name, "scope": _scope_label(h.scope_rule),
            "amount": h.amount, "batch_date": h.batch_date, "status": h.status,
            "reject_reason": h.reject_reason, "ttl_s": h.ttl_s,
            "started_at": iso(h.started_at), "expires_at": iso(h.expires_at),
            "finished_at": iso(h.finished_at)}


def voucher_dict(v: QuotaVoucher) -> dict:
    return {"id": v.id, "serial": v.serial, "account_id": v.account_id,
            "rule_name": v.rule_name, "scope": _scope_label(v.scope_rule),
            "amount": v.amount, "kind": v.kind,
            "occurred_at": iso(v.occurred_at), "batch_date": v.batch_date,
            "status": v.status, "suspend_reason": v.suspend_reason,
            "received_at": iso(v.received_at)}


def release_dict(r: QuotaRelease) -> dict:
    return {"id": r.id, "serial": r.serial, "hold_serial": r.hold_serial,
            "scope": _scope_label(r.scope_rule), "amount": r.amount,
            "batch_date": r.batch_date, "reason": r.reason,
            "ts": iso(r.ts), "reaped_by": r.reaped_by}


def settlement_dict(s: QuotaSettlement) -> dict:
    return {"id": s.id, "serial": s.serial, "hold_serial": s.hold_serial,
            "scope": _scope_label(s.scope_rule), "held_amount": s.held_amount,
            "actual_amount": s.actual_amount, "delta_amount": s.delta_amount,
            "batch_date": s.batch_date, "origin_batch_date": s.origin_batch_date,
            "over_limit": s.over_limit, "ts": iso(s.ts)}


def adjustment_dict(a: QuotaAdjustment, entry: QuotaAdjustmentEntry | None) -> dict:
    return {"id": a.id, "serial": a.serial, "account_id": a.account_id,
            "scope": _scope_label(a.scope_rule), "rule_name": a.rule_name,
            "batch_date": a.batch_date, "voucher_amount": a.voucher_amount,
            "requested_kind": a.requested_kind, "status": a.status,
            "reason": a.reason, "detail": a.detail,
            "created_at": iso(a.created_at), "decided_by": a.decided_by,
            "decided_at": iso(a.decided_at), "decision_note": a.decision_note,
            "entry": ({"id": entry.id, "amount": entry.amount,
                       "kind": entry.kind, "batch_date": entry.batch_date,
                       "limit_version": entry.limit_version,
                       "ts": iso(entry.ts)} if entry else None)}


def page_dict(page: QuotaPage, lines, session, acc: QuotaAccount,
              batch: QuotaBatch, with_appendix: bool = False) -> dict:
    out = {
        "page_id": page.id, "account": acc.name, "batch_date": page.batch_date,
        "sealed_at": iso(batch.sealed_at), "sealed_by": batch.sealed_by,
        "snapshot": page.snapshot,
        "lines": [{"line_no": ln.line_no, "serial": ln.serial,
                   "scope": _scope_label(ln.scope_rule),
                   "type": ln.line_type, "amount": ln.amount,
                   "detail": ln.detail} for ln in lines],
        "immutable": True,
    }
    if with_appendix:
        # 封账之后的附录：跨周期归还/销账/调整都不改账页，只在这里可追溯
        rels = session.query(QuotaRelease).filter_by(
            account_id=acc.id, batch_date=page.batch_date,
            ).filter(QuotaRelease.ts > batch.sealed_at).all()
        ents = session.query(QuotaAdjustmentEntry).filter_by(
            account_id=acc.id, batch_date=page.batch_date).all()
        pend = session.query(QuotaAdjustment).filter_by(
            account_id=acc.id, batch_date=page.batch_date,
            status=ADJ_PENDING).all()
        out["post_seal_appendix"] = {
            "releases_after_seal": [release_dict(r) for r in rels],
            "adjustment_entries": [
                {"serial": e.serial, "amount": e.amount, "kind": e.kind,
                 "limit_version": e.limit_version, "ts": iso(e.ts),
                 "confirmed_by": e.confirmed_by} for e in ents],
            "pending_adjustments": [a.id for a in pend],
        }
    return out
