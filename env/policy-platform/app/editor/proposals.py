"""变更管控路由（挂在 editor 服务）：提案提交 / 逐项差异 / 评审 / 撤回 /
立即或预约生效 / 完整时间线查询 / 审批角色配置。
"""
from fastapi import APIRouter, HTTPException, Query

from ..common import proposal_service as ps
from ..common.db import SessionLocal
from ..common.models import (ApprovalConfig, Proposal, ProposalEvent, Review)
from ..common.schemas import (ApprovalConfigUpsert, ProposalCreate, ReviewCreate,
                              WithdrawRequest)
from ..common.serialize import (_iso, proposal_dict, proposal_event_dict,
                                review_dict)

router = APIRouter(prefix="/proposals", tags=["proposals"])


def _err(e: ps.ProposalError):
    raise HTTPException(e.http, {"error": e.code, "detail": e.detail})


# ---------- 审批角色配置 ----------

@router.get("/approval-config")
def list_approval_configs():
    with SessionLocal() as s:
        out = []
        for cfg in s.query(ApprovalConfig).order_by(ApprovalConfig.policy_name).all():
            out.append({"policy": cfg.policy_name or None, "rules": cfg.rules,
                        "updated_by": cfg.updated_by,
                        "updated_at": _iso(cfg.updated_at)})
        return {"configs": out, "system_default": ps.DEFAULT_RULES}


@router.put("/approval-config/{policy_name}")
def upsert_policy_config(policy_name: str, req: ApprovalConfigUpsert):
    with SessionLocal() as s:
        try:
            return ps.upsert_config(s, policy_name=policy_name,
                                    rules=[r.model_dump() for r in req.rules],
                                    actor=req.actor)
        except ps.ProposalError as e:
            _err(e)


@router.put("/approval-config-default")
def upsert_default_config(req: ApprovalConfigUpsert):
    with SessionLocal() as s:
        try:
            return ps.upsert_config(s, policy_name=None,
                                    rules=[r.model_dump() for r in req.rules],
                                    actor=req.actor)
        except ps.ProposalError as e:
            _err(e)


# ---------- 提案 ----------

@router.post("", status_code=201)
def create_proposal(req: ProposalCreate):
    with SessionLocal() as s:
        try:
            p = ps.create_proposal(
                s, title=req.title, proposer=req.actor,
                changes_in=[c.model_dump() for c in req.changes],
                scheduled_at=req.scheduled_at, expires_at=req.expires_at)
        except ps.ProposalError as e:
            _err(e)
        return proposal_dict(p, include_changes=True)


@router.get("")
def list_proposals(status: str = None, policy: str = None,
                   limit: int = Query(default=100, le=500)):
    with SessionLocal() as s:
        q = s.query(Proposal).order_by(Proposal.id.desc())
        if status:
            q = q.filter_by(status=status)
        out = []
        for p in q.limit(limit).all():
            if policy and policy not in p.baseline.get("policies", []):
                continue
            out.append(proposal_dict(p))
        return out


@router.get("/{proposal_id}")
def get_proposal(proposal_id: int):
    with SessionLocal() as s:
        p = s.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, f"proposal {proposal_id} not found")
        return proposal_dict(p, include_changes=True)


@router.get("/{proposal_id}/diff")
def proposal_diff(proposal_id: int):
    """提交时固定的逐项语义差异（按变更项）。"""
    with SessionLocal() as s:
        p = s.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, f"proposal {proposal_id} not found")
        return {
            "proposal_id": proposal_id,
            "baseline_policy_versions": p.baseline.get("policy_versions"),
            "changes": [
                {"fragment": c["fragment"], "action": c["action"],
                 "base_version": c["base_version"], "base_body": c["base_body"],
                 "new_body": c["new_body"], "diff": c["diff"]}
                for c in p.changes
            ],
        }


@router.post("/{proposal_id}/reviews")
def review_proposal(proposal_id: int, req: ReviewCreate):
    with SessionLocal() as s:
        try:
            return ps.add_review(s, proposal_id, reviewer=req.actor, role=req.role,
                                 decision=req.decision, comment=req.comment)
        except ps.ProposalError as e:
            _err(e)


@router.get("/{proposal_id}/reviews")
def list_reviews(proposal_id: int):
    with SessionLocal() as s:
        if not s.get(Proposal, proposal_id):
            raise HTTPException(404, f"proposal {proposal_id} not found")
        rows = s.query(Review).filter_by(proposal_id=proposal_id).order_by(Review.id).all()
        return [review_dict(r) for r in rows]


@router.post("/{proposal_id}/withdraw")
def withdraw_proposal(proposal_id: int, req: WithdrawRequest):
    with SessionLocal() as s:
        try:
            return ps.withdraw(s, proposal_id, actor=req.actor)
        except ps.ProposalError as e:
            _err(e)


@router.post("/{proposal_id}/apply")
def apply_now(proposal_id: int, req: WithdrawRequest):
    """手动让已通过且到点（或未预约）的 SCHEDULED 提案立即生效；未到点拒绝。

    重复调用幂等：已生效的提案不产生新产物/记录，直接返回 already_effective。
    """
    with SessionLocal() as s:
        try:
            return ps.claim_and_apply(SessionLocal, proposal_id,
                                      actor=req.actor, trigger="manual")
        except ps.ProposalError as e:
            _err(e)


@router.get("/{proposal_id}/timeline")
def proposal_timeline(proposal_id: int):
    """完整时间线：提交 -> 每位评审人决定 -> 拒绝/撤回/过期/冲突 -> 生效产物版本。

    含提交时固定的逐项差异、评审人决定、最终进入运行环境的产物 (policy/version/hash)。
    """
    with SessionLocal() as s:
        p = s.get(Proposal, proposal_id)
        if not p:
            raise HTTPException(404, f"proposal {proposal_id} not found")
        reviews = [review_dict(r) for r in
                   s.query(Review).filter_by(proposal_id=proposal_id)
                   .order_by(Review.id).all()]
        events_rows = [proposal_event_dict(e) for e in
                       s.query(ProposalEvent).filter_by(proposal_id=proposal_id)
                       .order_by(ProposalEvent.id).all()]
        return {
            "proposal": proposal_dict(p),
            "pinned_diff": [
                {"fragment": c["fragment"], "base_version": c["base_version"],
                 "diff": c["diff"]} for c in p.changes],
            "reviews": reviews,
            "timeline": events_rows,
            "runtime_artifacts": p.applied,
        }
