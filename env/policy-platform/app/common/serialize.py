"""序列化辅助：ORM -> API 字典，以及查询输入摘要。"""
import hashlib
import json
from datetime import timezone


def _iso(dt):
    """统一输出带时区的 ISO8601（SQLite 取回的 naive datetime 按 UTC 处理）。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def summarize_inputs(inputs: dict) -> dict:
    """输入摘要：键列表 + 规范 JSON 的哈希 + 截断预览（不落完整输入也能审计）。"""
    canon = json.dumps(inputs, sort_keys=True, default=str, ensure_ascii=False)
    return {
        "keys": sorted(inputs.keys()),
        "sha256": hashlib.sha256(canon.encode()).hexdigest(),
        "size_bytes": len(canon.encode()),
        "preview": canon[:512],
    }


def frag_dict(f) -> dict:
    return {
        "name": f.name,
        "version": f.version,
        "refs": f.refs,
        "content_hash": f.content_hash,
        "body": f.body,
        "updated_by": f.updated_by,
        "updated_at": _iso(f.updated_at),
    }


def policy_dict(p) -> dict:
    return {
        "name": p.name,
        "entry_fragment": p.entry_fragment,
        "description": p.description,
        "latest_version": p.latest_version,
        "created_at": _iso(p.created_at),
    }


def artifact_dict(a) -> dict:
    return {
        "policy": a.policy_name,
        "version": a.version,
        "hash": a.hash,
        "entry_node": a.entry_node,
        "dep_chain": a.dep_chain,
        "revoked": a.revoked,
        "revoke_reason": a.revoke_reason,
        "revoked_by": a.revoked_by,
        "created_at": _iso(a.created_at),
    }


def decision_dict(d) -> dict:
    return {
        "id": d.id,
        "ts": _iso(d.ts),
        "request_id": d.request_id,
        "policy": d.policy,
        "requested_min_version": d.requested_min_version,
        "used_version": d.used_version,
        "below_min_version": d.below_min_version,
        "fallback_from": d.fallback_from,
        "fallback_reason": d.fallback_reason,
        "input_summary": d.input_summary,
        "result": d.result,
        "error": d.error,
        "latency_ms": d.latency_ms,
        "dep_chain": d.dep_chain,
    }


def audit_dict(e) -> dict:
    return {
        "id": e.id,
        "ts": _iso(e.ts),
        "service": e.service,
        "event_type": e.event_type,
        "actor": e.actor,
        "payload": e.payload,
    }


def proposal_dict(p, *, include_changes=False) -> dict:
    out = {
        "id": p.id,
        "title": p.title,
        "status": p.status,
        "proposer": p.proposer,
        "scheduled_at": _iso(p.scheduled_at),
        "expires_at": _iso(p.expires_at),
        "created_at": _iso(p.created_at),
        "decided_at": _iso(p.decided_at),
        "effective_at": _iso(p.effective_at),
        "impacted_policies": p.baseline.get("policies"),
        "approval_rules": p.baseline.get("approval_rules"),
        "changes": [
            {"fragment": c["fragment"], "action": c["action"],
             "base_version": c["base_version"], "base_hash": c["base_hash"],
             "new_hash": c["new_hash"],
             "change_count": c["diff"]["change_count"]}
            for c in p.changes
        ],
        "applied": p.applied,
        "conflict_reason": p.conflict_reason,
    }
    if include_changes:
        out["baseline"] = p.baseline
        out["changes"] = p.changes  # 含 base_body/new_body/逐项 diff
    return out


def review_dict(r) -> dict:
    return {
        "id": r.id,
        "proposal_id": r.proposal_id,
        "reviewer": r.reviewer,
        "role": r.role,
        "decision": r.decision,
        "comment": r.comment,
        "ts": _iso(r.created_at),
    }


def proposal_event_dict(e) -> dict:
    return {
        "id": e.id,
        "ts": _iso(e.ts),
        "event_type": e.event_type,
        "actor": e.actor,
        "payload": e.payload,
    }
