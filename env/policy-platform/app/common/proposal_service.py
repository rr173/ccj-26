"""变更管控核心（HTTP 无关）：提案 -> 评审 -> 立即/预约生效。

关键不变量：
- 提交即固化：每个变更项固定基线片段版本/哈希/正文、逐项语义差异；
  baseline 固定整个影响闭包（片段版本/哈希 + 受影响策略的当前产物版本）+ 生效所需角色规则。
- 评审：发起人不能审批自己；按固定的角色×人数规则；任一拒绝即拒绝；
  同一评审人重复审批幂等，不产生新记录；超过 expires_at 未达评审条件即过期。
- 生效：同策略多提案按 (scheduled_at, id) 确定顺序；执行前逐项核对基线，
  基线漂移（片段/产物版本变化）即停止并把提案标为 CONFLICT，冲突说明给出漂移项，
  已给出的评审意见原样保留；重复执行幂等。
- 预约与评审状态全部在数据库，调度器只是执行者，服务重启后未到点的预约继续生效、
  等待中的评审继续可处理。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy import update

from . import compile_service, compiler_core, dsl, events, semdiff
from .models import (ApprovalConfig, Fragment, Policy, Proposal,
                     ProposalEvent, Review, utcnow)

SERVICE = "editor"

IN_REVIEW = "IN_REVIEW"
SCHEDULED = "SCHEDULED"
APPLYING = "APPLYING"
EFFECTIVE = "EFFECTIVE"
REJECTED = "REJECTED"
WITHDRAWN = "WITHDRAWN"
EXPIRED = "EXPIRED"
CONFLICT = "CONFLICT"
APPLY_FAILED = "APPLY_FAILED"

PENDING_STATES = (IN_REVIEW, SCHEDULED)

# 未配置任何审批规则时的系统默认：1 名非发起人
DEFAULT_RULES = [{"role": "approver", "count": 1}]

# SQLite 没有行锁，进程内用互斥串行化生效动作（测试为单进程）
_apply_lock = threading.RLock()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ProposalError(Exception):
    def __init__(self, code: str, http: int = 422, detail=None):
        self.code = code
        self.http = http
        self.detail = detail or code
        super().__init__(code)


class _VF:
    """编译用的片段视图（提交时预演用，内容尚未落库）。"""

    def __init__(self, body, version=0, content_hash=None):
        self.name = None
        self.body = body
        self.refs = sorted(dsl.extract_refs(body))
        self.version = version
        self.content_hash = content_hash or dsl.canonical_hash(body)


def _event(session, p: Proposal, etype: str, actor=None, audit: bool = True, **payload):
    session.add(ProposalEvent(proposal_id=p.id, event_type=etype,
                              actor=actor or "system", payload=payload))
    if audit:
        events.audit(session, SERVICE, f"PROPOSAL_{etype}", actor,
                     proposal_id=p.id, **payload)


def _current_fragments(session) -> dict:
    return {f.name: f for f in session.query(Fragment).all()}


def _prospective_fragments(current: dict, changes: list) -> dict:
    """把提案变更叠加到当前片段上的内存视图（不写库）。"""
    view = dict(current)
    for ch in changes:
        view[ch["fragment"]] = _VF(ch["new_body"])
    return view


def _reachable(entry: str, frags: dict) -> tuple[list, list, list]:
    """容错的依赖闭包遍历：返回 (可达片段, 缺失引用)；环由编译期 resolve_order 报告。"""
    seen, missing, stack = set(), [], [entry]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        f = frags.get(name)
        if f is None:
            missing.append(name)
            continue
        seen.add(name)
        stack.extend(f.refs)
    return sorted(seen), missing


def _impacted_policies(session, changed: set, view: dict) -> list[str]:
    """受影响策略：提案后依赖闭包中包含任一变更片段的策略（含入口片段被改）。"""
    out = set()
    for p in session.query(Policy).all():
        reach, _ = _reachable(p.entry_fragment, view)
        if p.entry_fragment in changed or changed & set(reach):
            out.add(p.name)
    return sorted(out)


def _resolve_rules(session, policies: list) -> list:
    """生效所需角色规则：全局默认 ∪ 各受影响策略专属规则；同角色取最大人数。"""
    cfg = session.query(ApprovalConfig).filter_by(policy_name="").first()
    merged: dict = {}
    for r in (cfg.rules if cfg else DEFAULT_RULES):
        merged[r["role"]] = int(r["count"])
    for name in policies:
        pc = session.query(ApprovalConfig).filter_by(policy_name=name).first()
        for r in (pc.rules if pc else []):
            merged[r["role"]] = max(merged.get(r["role"], 0), int(r["count"]))
    return [{"role": role, "count": n} for role, n in sorted(merged.items())]


# ---------------------------------------------------------------------------
# 提交提案
# ---------------------------------------------------------------------------

def create_proposal(session, *, title: str, proposer: str, changes_in: list,
                    scheduled_at: datetime | None = None,
                    expires_at: datetime | None = None) -> Proposal:
    if not changes_in:
        raise ProposalError("empty_changes", 422, "proposal must contain >=1 change")
    now = utcnow()
    scheduled_at = _aware(scheduled_at)
    expires_at = _aware(expires_at)
    if expires_at is not None and expires_at <= now:
        raise ProposalError("expires_in_past", 422, "expires_at must be in the future")
    if scheduled_at is not None and scheduled_at <= now:
        raise ProposalError("schedule_in_past", 422,
                            "scheduled_at must be in the future; approve without a schedule to apply now")

    current = _current_fragments(session)
    changes, changed_names = [], set()
    for item in changes_in:
        name, body = item["fragment"], item["body"]
        if name in changed_names:
            raise ProposalError("duplicate_change", 422,
                                f"fragment '{name}' appears more than once")
        changed_names.add(name)
        try:
            dsl.validate_expr(body)
        except dsl.DSLError as e:
            raise ProposalError("invalid_dsl", 422, f"fragment '{name}': {e}")
        old = current.get(name)
        new_hash = dsl.canonical_hash(body)
        if old is not None and old.content_hash == new_hash:
            raise ProposalError("no_material_change", 422,
                                f"fragment '{name}' body is identical to current version {old.version}")
        diff = semdiff.semantic_diff(old.body if old else None, body)
        changes.append({
            "fragment": name,
            "action": "create" if old is None else "update",
            "base_version": old.version if old else 0,
            "base_hash": old.content_hash if old else None,
            "base_body": old.body if old else None,
            "new_body": body,
            "new_hash": new_hash,
            "diff": diff,
        })

    view = _prospective_fragments(current, changes)

    # 影响范围
    impacted = _impacted_policies(session, changed_names, view)
    if not impacted:
        raise ProposalError("no_impacted_policy", 422,
                            "changes are not reachable from any existing policy")

    # 预演编译：提案生效后每个受影响策略都必须可编译（环/缺失引用/DSL 错误在提交时拦截）
    preflight = []
    for pname in impacted:
        policy = session.query(Policy).filter_by(name=pname).first()
        try:
            art = compiler_core.build_artifact(pname, policy.entry_fragment, view)
            preflight.append({"policy": pname, "ok": True, "hash": art["hash"]})
        except (compiler_core.CompileError, dsl.DSLError) as e:
            raise ProposalError("preflight_failed", 422,
                                {"policy": pname, "error": str(e),
                                 "error_type": type(e).__name__})

    # 固化基线：影响闭包内全部片段的版本/哈希 + 受影响策略当前产物版本
    closure = set()
    for pname in impacted:
        policy = session.query(Policy).filter_by(name=pname).first()
        reach, _ = _reachable(policy.entry_fragment, view)
        closure |= set(reach)
    frag_pins = {}
    for fname in sorted(closure):
        f = current.get(fname)
        frag_pins[fname] = ({"version": f.version, "hash": f.content_hash}
                            if f else {"version": 0, "hash": None})
    policy_versions = {
        pname: session.query(Policy).filter_by(name=pname).first().latest_version
        for pname in impacted
    }
    baseline = {
        "policies": impacted,
        "fragments": frag_pins,
        "policy_versions": policy_versions,
        "approval_rules": _resolve_rules(session, impacted),
        "preflight": preflight,
    }

    p = Proposal(title=title or "", target_kind="fragment", status=IN_REVIEW,
                 proposer=proposer, changes=changes, baseline=baseline,
                 scheduled_at=scheduled_at, expires_at=expires_at)
    session.add(p)
    session.flush()
    p.seq = p.id
    _event(session, p, "SUBMITTED", proposer, title=p.title,
           changes=[{"fragment": c["fragment"], "action": c["action"],
                     "base_version": c["base_version"]} for c in changes],
           impacted_policies=impacted,
           scheduled_at=scheduled_at.isoformat() if scheduled_at else None,
           expires_at=expires_at.isoformat() if expires_at else None)
    session.commit()
    session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# 评审
# ---------------------------------------------------------------------------

def _quorum(rules: list, approved_reviews: list) -> dict:
    """按角色统计不同评审人。返回 {satisfied, per_role, missing}。"""
    per_role: dict = {}
    seen = set()
    for rv in approved_reviews:
        if rv.reviewer in seen:
            continue
        seen.add(rv.reviewer)
        per_role[rv.role] = per_role.get(rv.role, 0) + 1
    missing = [{"role": r["role"], "need": r["count"],
                "have": per_role.get(r["role"], 0)}
               for r in rules if per_role.get(r["role"], 0) < r["count"]]
    return {"satisfied": not missing, "per_role": per_role, "missing": missing}


def add_review(session, proposal_id: int, *, reviewer: str, role: str,
               decision: str, comment: str = "") -> dict:
    if decision not in ("approved", "rejected"):
        raise ProposalError("bad_decision", 422, "decision must be approved|rejected")
    with _apply_lock:
        p = session.get(Proposal, proposal_id)
        if p is None:
            raise ProposalError("not_found", 404, f"proposal {proposal_id} not found")
        if p.status != IN_REVIEW:
            raise ProposalError("not_in_review", 409,
                                {"status": p.status, "proposal_id": proposal_id})

        # 发起人不能审批自己的提案
        if reviewer == p.proposer:
            raise ProposalError("self_approval_forbidden", 403,
                                "proposer cannot review their own proposal")

        # 审批截止
        if p.expires_at and _aware(p.expires_at) <= utcnow():
            _expire(session, p, actor=reviewer)
            session.commit()
            raise ProposalError("proposal_expired", 409,
                                {"status": EXPIRED, "proposal_id": proposal_id})

        # 重复审批幂等：不产生第二条意见 / 事件 / 审计
        existing = (session.query(Review)
                    .filter_by(proposal_id=proposal_id, reviewer=reviewer).first())
        if existing is not None:
            return {"status": "already_reviewed", "proposal_id": proposal_id,
                    "decision": existing.decision, "review_id": existing.id}

        rv = Review(proposal_id=proposal_id, reviewer=reviewer, role=role,
                    decision=decision, comment=comment or "")
        session.add(rv)
        session.flush()
        _event(session, p, "REVIEWED", reviewer, role=role, decision=decision,
               comment=comment or "")

        if decision == "rejected":
            p.status = REJECTED
            p.decided_at = utcnow()
            _event(session, p, "REJECTED", reviewer, comment=comment or "")
            session.commit()
            return {"status": REJECTED, "proposal_id": proposal_id}

        rules = p.baseline.get("approval_rules", DEFAULT_RULES)
        approved = [r for r in session.query(Review)
                    .filter_by(proposal_id=proposal_id, decision="approved").all()]
        q = _quorum(rules, approved)
        if not q["satisfied"]:
            session.commit()
            return {"status": IN_REVIEW, "proposal_id": proposal_id,
                    "quorum": q["missing"]}

        # 评审通过
        p.decided_at = utcnow()
        sched = _aware(p.scheduled_at)
        if sched is not None and sched > utcnow():
            p.status = SCHEDULED
            _event(session, p, "SCHEDULED", reviewer, scheduled_at=sched.isoformat(),
                   quorum=q["per_role"])
            session.commit()
            return {"status": SCHEDULED, "proposal_id": proposal_id,
                    "scheduled_at": sched.isoformat()}

        # 立即生效（未预约或预约时间已到）
        return _do_apply(session, p, actor=reviewer, trigger="approval")


def withdraw(session, proposal_id: int, actor: str) -> dict:
    with _apply_lock:
        p = session.get(Proposal, proposal_id)
        if p is None:
            raise ProposalError("not_found", 404, f"proposal {proposal_id} not found")
        if actor != p.proposer:
            raise ProposalError("only_proposer", 403, "only the proposer can withdraw")
        if p.status not in PENDING_STATES:
            raise ProposalError("not_withdrawable", 409,
                                {"status": p.status, "proposal_id": proposal_id})
        p.status = WITHDRAWN
        p.decided_at = utcnow()
        _event(session, p, "WITHDRAWN", actor)
        session.commit()
        return {"status": WITHDRAWN, "proposal_id": proposal_id}


def _expire(session, p: Proposal, actor="system"):
    p.status = EXPIRED
    p.decided_at = utcnow()
    _event(session, p, "EXPIRED", actor,
           expires_at=_aware(p.expires_at).isoformat() if p.expires_at else None)


def sweep_expired(session) -> list[int]:
    """把超过审批截止仍在评审的提案标记为 EXPIRED（评审意见保留）。"""
    now = utcnow()
    ids = []
    for p in session.query(Proposal).filter_by(status=IN_REVIEW).all():
        if p.expires_at and _aware(p.expires_at) <= now:
            _expire(session, p)
            ids.append(p.id)
    if ids:
        session.commit()
    return ids


# ---------------------------------------------------------------------------
# 生效
# ---------------------------------------------------------------------------

def _baseline_drift(session, p: Proposal) -> dict:
    """核对提交时固定的基线与当前状态。返回漂移说明（空 dict = 无漂移）。"""
    current = _current_fragments(session)
    changed_names = {c["fragment"] for c in p.changes}
    frag_drift = []
    for c in p.changes:
        f = current.get(c["fragment"])
        if c["action"] == "create":
            if f is not None:
                frag_drift.append({"fragment": c["fragment"], "reason": "created_elsewhere",
                                   "pinned": "absent", "current_version": f.version,
                                   "current_hash": f.content_hash})
        elif f is None:
            frag_drift.append({"fragment": c["fragment"], "reason": "deleted",
                               "pinned_version": c["base_version"]})
        elif f.version != c["base_version"] or f.content_hash != c["base_hash"]:
            frag_drift.append({"fragment": c["fragment"], "reason": "fragment_changed",
                               "pinned_version": c["base_version"],
                               "pinned_hash": c["base_hash"],
                               "current_version": f.version,
                               "current_hash": f.content_hash})

    # 间接依赖：影响闭包内未被本提案直接修改的片段也不能变
    for fname, pin in sorted(p.baseline.get("fragments", {}).items()):
        if fname in changed_names:
            continue
        f = current.get(fname)
        cur_hash = f.content_hash if f else None
        if cur_hash != pin["hash"]:
            frag_drift.append({"fragment": fname, "reason": "dependency_changed",
                               "pinned_version": pin["version"], "pinned_hash": pin["hash"],
                               "current_version": f.version if f else None,
                               "current_hash": cur_hash})

    policy_drift = []
    for pname, pin_v in p.baseline.get("policy_versions", {}).items():
        pol = session.query(Policy).filter_by(name=pname).first()
        if pol is None:
            policy_drift.append({"policy": pname, "reason": "deleted"})
        elif pol.latest_version != pin_v:
            policy_drift.append({"policy": pname, "reason": "baseline_moved",
                                 "pinned_version": pin_v,
                                 "current_version": pol.latest_version})
    if frag_drift or policy_drift:
        return {"fragment_drift": frag_drift, "policy_drift": policy_drift}
    return {}


def _do_apply(session, p: Proposal, *, actor: str, trigger: str) -> dict:
    """实际执行（调用方已持有 _apply_lock）。成功 EFFECTIVE，漂移 CONFLICT。"""
    impacted = p.baseline["policies"]

    # Postgres：锁定受影响策略行，保证同策略多提案的生效严格串行
    if session.get_bind().dialect.name == "postgresql":
        (session.query(Policy).filter(Policy.name.in_(impacted))
         .order_by(Policy.name).with_for_update().all())

    drift = _baseline_drift(session, p)
    if drift:
        reason = {"type": "baseline_changed", "trigger": trigger, **drift}
        p.status = CONFLICT
        p.conflict_reason = reason
        _event(session, p, "CONFLICT", actor, reason=reason)
        session.commit()
        return {"status": CONFLICT, "proposal_id": p.id, "conflict_reason": reason}

    current = _current_fragments(session)
    view = _prospective_fragments(current, p.changes)

    # 执行前再次预演（防御性）
    for pname in impacted:
        policy = session.query(Policy).filter_by(name=pname).first()
        try:
            compiler_core.build_artifact(pname, policy.entry_fragment, view)
        except (compiler_core.CompileError, dsl.DSLError) as e:
            p.status = APPLY_FAILED
            p.conflict_reason = {"type": "preflight_failed", "policy": pname,
                                 "error": str(e), "error_type": type(e).__name__}
            _event(session, p, "APPLY_FAILED", actor, reason=p.conflict_reason)
            session.commit()
            return {"status": APPLY_FAILED, "proposal_id": p.id,
                    "error": p.conflict_reason}

    # 落地片段变更
    for c in p.changes:
        old = current.get(c["fragment"])
        if old is None:
            session.add(Fragment(name=c["fragment"], version=1, body=c["new_body"],
                                 refs=sorted(dsl.extract_refs(c["new_body"])),
                                 content_hash=c["new_hash"], updated_by=actor))
            events.audit(session, SERVICE, "FRAGMENT_CREATED", actor,
                         name=c["fragment"], refs=sorted(dsl.extract_refs(c["new_body"])),
                         proposal_id=p.id)
        else:
            old.version += 1
            old.body = c["new_body"]
            old.refs = sorted(dsl.extract_refs(c["new_body"]))
            old.content_hash = c["new_hash"]
            old.updated_by = actor
            events.audit(session, SERVICE, "FRAGMENT_UPDATED", actor,
                         name=c["fragment"], proposal_id=p.id,
                         from_version=c["base_version"], to_version=old.version)
    session.flush()

    # 受影响策略重新编译，生成进入运行环境的不可变产物
    applied = []
    for pname in impacted:
        res = compile_service.compile_policy(session, pname, actor, SERVICE)
        applied.append({"policy": pname, "status": res.get("status"),
                        "version": res.get("version"), "hash": res.get("hash"),
                        "error": res.get("error")})

    failed = [a for a in applied if a["status"] not in ("published", "duplicate")]
    now = utcnow()
    if failed:
        p.status = APPLY_FAILED
        p.applied = applied
        p.conflict_reason = {"type": "compile_failed", "failures": failed}
        _event(session, p, "APPLY_FAILED", actor, applied=applied)
        session.commit()
        return {"status": APPLY_FAILED, "proposal_id": p.id, "applied": applied}

    p.status = EFFECTIVE
    p.applied = applied
    p.effective_at = now
    _event(session, p, "EFFECTIVE", actor, trigger=trigger, applied=applied,
           effective_at=now.isoformat())
    session.commit()

    # 本提案改变了基线：其他等待中的提案若固定基线已漂移，立即标冲突（意见保留）
    mark_drifted(session, trigger={"type": "proposal_effective",
                                   "proposal_id": p.id, "by": actor,
                                   "policies": impacted})

    return {"status": EFFECTIVE, "proposal_id": p.id, "applied": applied,
            "effective_at": now.isoformat()}


def claim_and_apply(session_factory, proposal_id: int, *, actor: str = "scheduler",
                    trigger: str = "schedule", allow_early: bool = False) -> dict:
    """调度器/手动入口：原子认领 SCHEDULED 提案后执行（防重复执行）。"""
    with _apply_lock:
        with session_factory() as s:
            p = s.get(Proposal, proposal_id)
            if p is None:
                raise ProposalError("not_found", 404, f"proposal {proposal_id} not found")
            if p.status == EFFECTIVE:
                return {"status": "already_effective", "proposal_id": proposal_id,
                        "applied": p.applied}
            if p.status == CONFLICT:
                return {"status": CONFLICT, "proposal_id": proposal_id,
                        "conflict_reason": p.conflict_reason}
            if p.status in (REJECTED, WITHDRAWN, EXPIRED, APPLY_FAILED):
                return {"status": "skipped", "proposal_id": proposal_id, "reason": p.status}
            if p.status != SCHEDULED:
                return {"status": "skipped", "proposal_id": proposal_id,
                        "reason": p.status}
            if not allow_early and _aware(p.scheduled_at) > utcnow():
                return {"status": "not_due", "proposal_id": proposal_id,
                        "scheduled_at": _aware(p.scheduled_at).isoformat()}

            # 原子认领：只有一个执行者能把 SCHEDULED -> APPLYING
            rowcount = (s.execute(
                update(Proposal).where(Proposal.id == proposal_id,
                                       Proposal.status == SCHEDULED)
                .values(status=APPLYING)).rowcount)
            s.commit()
            if rowcount == 0:
                s.expire_all()
                cur = s.get(Proposal, proposal_id)
                return {"status": "skipped", "proposal_id": proposal_id,
                        "reason": cur.status}

        with session_factory() as s:
            p = s.get(Proposal, proposal_id)
            return _do_apply(s, p, actor=actor, trigger=trigger)


def run_due(session_factory, *, actor: str = "scheduler") -> list:
    """一个调度周期：先清过期，再按 (scheduled_at, id) 的确定顺序执行到点提案。

    顺序保证：同策略多个到点提案按预约先后（再按 id 决胜）；排在前面的先生效、
    移动基线，后面的执行时基线核对失败 -> CONFLICT 停止并给出说明。
    """
    with session_factory() as s:
        sweep_expired(s)
        due = [p for p in s.query(Proposal).filter_by(status=SCHEDULED).all()
               if _aware(p.scheduled_at) <= utcnow()]
    due.sort(key=lambda p: (_aware(p.scheduled_at), p.id))
    return [claim_and_apply(session_factory, p.id, actor=actor) for p in due]


def recover_applying(session) -> int:
    """重启恢复：把崩溃残留的 APPLYING 复位为 SCHEDULED（执行有基线核对/去重兜底）。"""
    n = session.query(Proposal).filter_by(status=APPLYING).update({Proposal.status: SCHEDULED})
    session.commit()
    return n


def mark_drifted(session, *, trigger: dict) -> list[int]:
    """片段在提案之外再次变化 / 其他提案生效后：把固定基线已漂移的等待中提案标冲突。

    只改提案状态与时间线，Review 表完全不动 —— 已给出的意见保留。
    """
    conflicted = []
    for p in session.query(Proposal).filter(Proposal.status.in_(PENDING_STATES)).all():
        drift = _baseline_drift(session, p)
        if drift:
            p.status = CONFLICT
            p.conflict_reason = {"type": "dependency_changed", "trigger": trigger, **drift}
            _event(session, p, "CONFLICT", trigger.get("by", "system"),
                   reason=p.conflict_reason)
            conflicted.append(p.id)
    if conflicted:
        session.commit()
    return conflicted


# ---------------------------------------------------------------------------
# 审批配置
# ---------------------------------------------------------------------------

def validate_rules(rules: list) -> list:
    if not isinstance(rules, list) or not rules:
        raise ProposalError("bad_rules", 422, "rules must be a non-empty list")
    norm = {}
    for r in rules:
        role = r.get("role")
        count = r.get("count")
        if not isinstance(role, str) or not role.strip():
            raise ProposalError("bad_rules", 422, "rule.role must be non-empty string")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ProposalError("bad_rules", 422, f"rule.count for {role} must be >=1")
        norm[role] = max(norm.get(role, 0), count)
    return [{"role": k, "count": v} for k, v in sorted(norm.items())]


def upsert_config(session, *, policy_name: str | None, rules: list, actor: str) -> dict:
    rules = validate_rules(rules)
    key = policy_name or ""   # 空串哨兵 = 全局默认
    cfg = session.query(ApprovalConfig).filter_by(policy_name=key).first()
    if cfg is None:
        cfg = ApprovalConfig(policy_name=key, rules=rules, updated_by=actor)
        session.add(cfg)
    else:
        cfg.rules = rules
        cfg.updated_by = actor
    events.audit(session, SERVICE, "APPROVAL_CONFIG_UPDATED", actor,
                 policy=policy_name, rules=rules)
    session.commit()
    return {"policy": policy_name, "rules": rules}


def get_config(session, policy_name: str | None) -> dict:
    cfg = session.query(ApprovalConfig).filter_by(policy_name=policy_name or "").first()
    if cfg is None:
        return {"policy": policy_name, "rules": None,
                "effective_default": DEFAULT_RULES}
    return {"policy": policy_name, "rules": cfg.rules}
