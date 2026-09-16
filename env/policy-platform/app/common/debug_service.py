"""逐步调试会话核心逻辑（与 FastAPI 解耦，便于单测）。

领域规则（详见 README「策略逐步调试」一节）：

- **固定版本**：会话创建时把产物 nodes/topo/entry/dep_chain/hash 整体快照进会话；
  之后即使原产物被撤销或发布了新版本，调试始终使用快照，版本状态实时标注。
- **脱敏输入**：原始输入不落库（仅存键名/哈希/大小），递归按敏感键名脱敏后再调试。
- **实际求值顺序**：按产物 topo（依赖在前）逐节点推进；step 只推进一步，
  continue 在后台线程推进到下一断点 / 暂停请求 / 错误 / 结束。
- **断点**：node（节点 id）/ op（算子类型）/ condition（DSL 布尔表达式，可限定节点）。
- **错误帧**：确定性错误（缺输入、除零、算子未加载…）停在出错节点，frame 记录错误，
  分支状态 error，会话不消失；可从此处分叉修改输入重试。
- **分叉**：从任意停止点（暂停 / 错误 / 完成）分叉，父分支帧只追加、永不改写。
- **租约**：同一时刻只有持有人（holder + token，带到期时间）能推进；到期可被接管，
  旧持有人（token 不匹配）的命令一律拒绝；后台推进在节点边界感知接管并停下。
- **幂等 / 防乱序**：cmd_id 全局去重（重复命令不重复推进）；分支级 seq，
  乱序命令返回 409 unexpected_seq + 当前期望序号。
- **重启续跑**：所有状态在数据库；启动时把崩溃残留的 running 分支复位为 paused 并留痕。
"""
from __future__ import annotations

import copy
import json
import secrets
import threading

from . import dsl
from .config import DEBUG_DEFAULT_LEASE_S, DEBUG_MAX_LEASE_S
from .db import SessionLocal
from .models import (Artifact, DebugBranch, DebugCommand, DebugEpoch, DebugEvent,
                     DebugFrame, DebugSession, Policy, utcnow)
from .serialize import _iso, summarize_inputs

# 递归脱敏时默认视为敏感的键名（小写匹配）。维护者可在创建会话时追加自定义键名。
DEFAULT_SECRET_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "credentials",
    "id_card", "idcard", "phone", "mobile", "email", "bank_card",
}
REDACTED = "***REDACTED***"


class DebugError(Exception):
    """业务错误：携带 HTTP 状态码、机器可读错误码与附加上下文。"""

    def __init__(self, status: int, code: str, message: str | None = None, **extra):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.extra = extra

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #

def redact_inputs(inputs: dict, extra_keys: set[str] | None = None):
    """递归把敏感键名对应的值替换为固定掩码。

    返回 (脱敏后输入, 报告)。报告含被掩码的 JSON 路径、使用的敏感键集合，
    以及原始输入的最小化摘要（键名 + 哈希 + 大小，不含正文）。
    """
    patterns = set(DEFAULT_SECRET_KEYS) | {k.lower() for k in (extra_keys or set())}
    masked_paths: list[str] = []

    def walk(obj, path: str):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                p = f"{path}.{k}" if path else f"$.{k}"
                if isinstance(k, str) and k.lower() in patterns:
                    out[k] = REDACTED
                    masked_paths.append(p)
                else:
                    out[k] = walk(v, p)
            return out
        if isinstance(obj, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(obj)]
        return obj

    redacted = walk(inputs, "$")
    report = {
        "masked_paths": sorted(masked_paths),
        "secret_keys": sorted(patterns),
        "original_input_summary": summarize_inputs(inputs or {}),
    }
    return redacted, report


# --------------------------------------------------------------------------- #
# 断点
# --------------------------------------------------------------------------- #

VALID_BP_TYPES = {"node", "op", "condition"}


def validate_breakpoint(bp: dict, topo: list) -> None:
    if not isinstance(bp, dict):
        raise DebugError(422, "invalid_breakpoint", "breakpoint must be an object")
    btype = bp.get("type")
    if btype not in VALID_BP_TYPES:
        raise DebugError(422, "invalid_breakpoint",
                         f"breakpoint type must be one of {sorted(VALID_BP_TYPES)}",
                         breakpoint=bp)
    if btype == "node":
        if not isinstance(bp.get("node"), str) or bp["node"] not in topo:
            raise DebugError(422, "unknown_breakpoint_node",
                             f"node '{bp.get('node')}' not in this artifact",
                             nodes=topo)
    elif btype == "op":
        if not isinstance(bp.get("op"), str) or not bp["op"]:
            raise DebugError(422, "invalid_breakpoint",
                             "'op' breakpoint requires a non-empty op name",
                             breakpoint=bp)
    else:
        expr = bp.get("expr")
        try:
            # 复用 DSL 静态校验：算子必须是本 runtime 注册的算子
            dsl.validate_expr(expr, ops=dsl.OPS)
            if _expr_contains_ref(expr):
                raise dsl.DSLError(
                    "condition breakpoints cannot reference fragments "
                    "({'ref'} is not supported here)")
        except dsl.DSLError as e:
            raise DebugError(422, "invalid_breakpoint_condition", str(e), breakpoint=bp)
        if bp.get("node") is not None and bp["node"] not in topo:
            raise DebugError(422, "unknown_breakpoint_node",
                             f"node '{bp['node']}' not in this artifact", nodes=topo)


def _expr_contains_ref(expr) -> bool:
    """条件断点表达式不允许片段引用（条件只对输入求值，无片段命名空间）。"""
    if isinstance(expr, dict):
        if "ref" in expr:
            return True
        return any(_expr_contains_ref(v) for v in expr.values())
    if isinstance(expr, list):
        return any(_expr_contains_ref(x) for x in expr)
    return False


def _eval_condition(expr, inputs: dict):
    """条件断点表达式求值（输入变量缺失按 None，不报错）。求值异常由调用方按不命中处理。"""
    if isinstance(expr, dict):
        if "op" in expr:
            fn = dsl.OPS[expr["op"]][0]
            return fn([_eval_condition(a, inputs) for a in expr["args"]])
        if "var" in expr:
            if expr["var"] in inputs:
                return inputs[expr["var"]]
            if "default" in expr:
                return _eval_condition(expr["default"], inputs)
            return None
        raise dsl.DSLError("unsupported expression in condition")
    if isinstance(expr, list):
        return [_eval_condition(x, inputs) for x in expr]
    return expr


def _normalise_bp(bp: dict) -> dict:
    out = {"type": bp["type"], "enabled": bool(bp.get("enabled", True))}
    if bp["type"] == "node":
        out["node"] = bp["node"]
    elif bp["type"] == "op":
        out["op"] = bp["op"]
    else:
        out["expr"] = bp["expr"]
        out["node"] = bp.get("node")
    return out


def _bp_matches(bp: dict, session: DebugSession, branch: DebugBranch,
                nid: str, node: dict) -> tuple[bool, str | None]:
    """节点执行前检查断点。返回 (是否命中, 说明)。条件表达式自身出错按不命中处理。"""
    if not bp.get("enabled", True):
        return False, None
    btype = bp["type"]
    if btype == "node":
        return (nid == bp["node"], f"node breakpoint at {nid}") if nid == bp["node"] else (False, None)
    if btype == "op":
        if node.get("kind") == "op" and node.get("op") == bp["op"]:
            return True, f"op breakpoint '{bp['op']}' at {nid}"
        return False, None
    # condition
    if bp.get("node") and bp["node"] != nid:
        return False, None
    try:
        hit = bool(_eval_condition(bp["expr"], branch.inputs))
    except Exception as e:  # noqa: BLE001 - 坏条件不应中断调试推进
        return False, f"condition error at {nid} (treated as no-match): {e}"
    return (hit, f"condition breakpoint matched at {nid}") if hit else (False, None)


# --------------------------------------------------------------------------- #
# 节点执行（与 dsl.evaluate 的语义保持一致，但一次只执行一个节点）
# --------------------------------------------------------------------------- #

def _execute_node(nodes: dict, nid: str, inputs: dict, values: dict):
    """执行单个节点，返回 (args_in, value)；确定性错误抛 DSLError。"""
    node = nodes[nid]
    kind = node["kind"]
    if kind == "const":
        return [], node["value"]
    if kind == "var":
        if node["name"] in inputs:
            return [], inputs[node["name"]]
        if "default" in node:
            return [], node["default"]
        raise dsl.MissingInput(f"missing input: {node['name']}")
    if kind == "list":
        args_in = [values[a] for a in node["args"]]
        return args_in, list(args_in)
    if kind == "op":
        op = node["op"]
        if op not in dsl.OPS:
            # 与运行时「节点未加载」一致：调试器是确定性执行，停成错误帧而非会话消失
            raise dsl.DSLError(f"op '{op}' not loaded at {nid}")
        args_in = [values[a] for a in node["args"]]
        try:
            return args_in, dsl.OPS[op][0](args_in)
        except dsl.DSLError:
            raise
        except Exception as e:  # noqa: BLE001 - 与 evaluate 一致，统一包装
            raise dsl.DSLError(f"op '{op}' failed at {nid}: {e}") from e
    raise dsl.DSLError(f"unknown node kind '{kind}' at {nid}")


def _classify_error(e: Exception, nid: str) -> dict:
    if isinstance(e, dsl.MissingInput):
        etype = "missing_input"
    elif "not loaded" in str(e):
        etype = "op_not_loaded"
    else:
        etype = "dsl_error"
    return {"type": etype, "node": nid, "message": str(e)}


# --------------------------------------------------------------------------- #
# 存取辅助
# --------------------------------------------------------------------------- #

def _event(s, session_id: int, event_type: str, actor="anonymous",
           branch_id=None, **payload) -> None:
    s.add(DebugEvent(session_id=session_id, branch_id=branch_id,
                     event_type=event_type, actor=actor or "anonymous", payload=payload))


def _get_session(s, session_id: int) -> DebugSession:
    session = s.get(DebugSession, session_id)
    if not session:
        raise DebugError(404, "session_not_found",
                         f"debug session {session_id} not found")
    return session


def _get_branch(s, session: DebugSession, branch_id: str | None) -> DebugBranch:
    bid = branch_id or session.root_branch_id
    branch = (s.query(DebugBranch)
              .filter_by(session_id=session.id, branch_id=bid).first())
    if not branch:
        raise DebugError(404, "branch_not_found",
                         f"branch '{bid}' not found in session {session.id}")
    return branch


def _active(session: DebugSession):
    if session.status == "ended":
        raise DebugError(409, "session_ended",
                         f"session {session.id} has ended: {session.end_reason}",
                         end_reason=session.end_reason)


def _lease_active(session: DebugSession) -> bool:
    if session.lease_expires_at is None:
        return False
    expires = session.lease_expires_at
    if expires.tzinfo is None:  # SQLite 取回 naive datetime，按 UTC 处理
        from datetime import timezone
        expires = expires.replace(tzinfo=timezone.utc)
    return utcnow() < expires


def _require_lease(session: DebugSession, actor: str, token: str | None) -> None:
    """变更类命令统一门禁：会话未结束 + 租约未到期 + 持有人与 token 匹配。"""
    _active(session)
    if not _lease_active(session):
        raise DebugError(409, "lease_expired",
                         f"lease held by '{session.lease_holder}' has expired; "
                         "call /lease to take over",
                         lease_holder=session.lease_holder)
    if actor != session.lease_holder or not token or token != session.lease_token:
        raise DebugError(409, "not_lease_holder",
                         f"session is currently leased to '{session.lease_holder}'; "
                         "observers may only GET",
                         lease_holder=session.lease_holder)


def _no_running_branch(s, session: DebugSession, *, exclude: str | None = None) -> None:
    q = s.query(DebugBranch).filter_by(session_id=session.id, status="running")
    if exclude:
        q = q.filter(DebugBranch.branch_id != exclude)
    running = q.first()
    if running:
        raise DebugError(409, "branch_running",
                         f"branch '{running.branch_id}' is running; pause it first "
                         "(or wait for its current continue to stop)",
                         running_branch=running.branch_id)


def _dedupe_command(s, session: DebugSession, cmd_id: str | None,
                    command: str, actor: str, branch_id: str | None):
    """cmd_id 幂等：返回 (命令行, 是否为已存在的重复命令)。

    重复命令调用方直接回放其缓存响应（不重复推进）；新命令返回新建行。
    """
    if not cmd_id:
        return None, False
    existing = (s.query(DebugCommand)
                .filter_by(session_id=session.id, cmd_id=cmd_id).first())
    if existing:
        if existing.command != command or existing.actor != actor:
            raise DebugError(409, "cmd_id_conflict",
                             f"cmd_id '{cmd_id}' already used for command "
                             f"'{existing.command}' by '{existing.actor}'")
        return existing, True
    cmd = DebugCommand(session_id=session.id, branch_id=branch_id, cmd_id=cmd_id,
                       command=command, actor=actor)
    s.add(cmd)
    s.flush()  # 让随后的 .one()/.query 能取到新命令行
    return cmd, False


def _load_values(s, session: DebugSession, branch: DebugBranch) -> dict:
    """从只追加帧重建各节点已得值（错误帧无值，分支停在该节点不会再往后走）。"""
    frames = (s.query(DebugFrame)
              .filter_by(session_id=session.id, branch_id=branch.branch_id)
              .order_by(DebugFrame.index).all())
    return {f.node_id: f.result for f in frames if f.error is None}


def _remaining_path(session: DebugSession, branch: DebugBranch) -> list:
    """尚未求值的节点 id（错误状态下当前出错节点也包含在内，便于观察）。"""
    pos = branch.position
    if branch.status == "error":
        return session.topo[pos:]
    return session.topo[pos:]


def _current_node(session: DebugSession, branch: DebugBranch,
                  values: dict | None = None) -> dict | None:
    """暂停/错误时当前节点的视图：节点定义 + 执行前可见入参 + 剩余路径。"""
    topo = session.topo
    if branch.status == "completed":
        return None
    idx = branch.position
    if idx >= len(topo):
        return None
    nid = topo[idx]
    node = session.nodes[nid]
    out = {"index": idx, "node_id": nid, "kind": node["kind"],
           "remaining_path": topo[idx + 1:]}
    if node.get("kind") == "op":
        out["op"] = node["op"]
        out["arg_nodes"] = node["args"]
    elif node.get("kind") == "var":
        out["var"] = node["name"]
        out["has_default"] = "default" in node
    elif node.get("kind") == "list":
        out["arg_nodes"] = node["args"]
    if values is not None and node.get("kind") in ("op", "list"):
        out["args_in"] = [values.get(a) for a in node["args"]]
    else:
        out["args_in"] = []
    return out


def _version_status(s, session: DebugSession) -> dict:
    """固定版本当前的生命周期状态（不影响调试执行，仅明确提示）。"""
    art = (s.query(Artifact)
           .filter_by(policy_name=session.policy_name,
                      version=session.pinned_version).first())
    latest = (s.query(Artifact)
              .filter_by(policy_name=session.policy_name)
              .order_by(Artifact.version.desc()).first())
    latest_version = latest.version if latest else None
    if art is None:
        return {"code": "missing", "pinned_version": session.pinned_version,
                "latest_version": latest_version,
                "note": "pinned artifact row no longer exists; "
                        "session continues on its frozen snapshot"}
    newer = [a.version for a in (s.query(Artifact)
             .filter_by(policy_name=session.policy_name)
             .filter(Artifact.version > session.pinned_version)
             .order_by(Artifact.version).all())]
    if art.revoked:
        code, note = "revoked", (
            f"artifact v{art.version} has been REVOKED"
            f"（{art.revoke_reason or 'no reason'}）；会话仍固定使用该版本快照")
    elif newer:
        code, note = "outdated", (
            f"newer version(s) {newer} exist; session stays pinned to v{art.version}")
    else:
        code, note = "active", f"v{art.version} is the latest, non-revoked version"
    return {
        "code": code, "note": note,
        "pinned_version": art.version, "pinned_hash": session.pinned_hash,
        "hash_intact": art.hash == session.pinned_hash,
        "revoked": art.revoked, "revoke_reason": art.revoke_reason,
        "revoked_by": art.revoked_by,
        "latest_version": latest_version, "newer_versions": newer,
    }


def _lease_dict(session: DebugSession) -> dict:
    return {"holder": session.lease_holder, "expires_at": _iso(session.lease_expires_at),
            "ttl_s": session.lease_ttl_s, "expired": not _lease_active(session)}


def frame_dict(f: DebugFrame) -> dict:
    return {"index": f.index, "node_id": f.node_id, "ts": _iso(f.ts),
            "args_in": f.args_in, "result": f.result, "error": f.error,
            "duration_ms": f.duration_ms}


def branch_dict(s, session: DebugSession, branch: DebugBranch,
                *, frames: bool = False) -> dict:
    values = _load_values(s, session, branch) if branch.status in ("paused", "error") else None
    out = {
        "branch_id": branch.branch_id,
        "parent_branch_id": branch.parent_branch_id,
        "fork_index": branch.fork_index,
        "fork_patch": branch.fork_patch,
        "position": branch.position,
        "total_nodes": len(session.topo),
        "status": branch.status,
        "pause_requested": branch.pause_requested,
        "error": branch.error,
        "final_result": branch.final_result,
        "remaining_path": _remaining_path(session, branch),
        "current": _current_node(session, branch, values),
        "breakpoints": branch.breakpoints,
        "next_seq": branch.next_seq,
        "created_at": _iso(branch.created_at),
        "updated_at": _iso(branch.updated_at),
    }
    if frames:
        fs = (s.query(DebugFrame)
              .filter_by(session_id=session.id, branch_id=branch.branch_id)
              .order_by(DebugFrame.index).all())
        out["frames"] = [frame_dict(f) for f in fs]
    return out


def session_dict(s, session: DebugSession, *, include_inputs: bool = True) -> dict:
    branches = (s.query(DebugBranch).filter_by(session_id=session.id)
                .order_by(DebugBranch.id).all())
    out = {
        "id": session.id,
        "title": session.title,
        "policy": session.policy_name,
        "status": session.status,
        "end_reason": session.end_reason,
        "artifact": {
            "pinned_version": session.pinned_version,
            "pinned_hash": session.pinned_hash,
            "dep_chain": session.dep_chain,
            "entry_node": session.entry_node,
            "version_status": _version_status(s, session),
        },
        "input_redaction": session.input_redaction,
        "lease": _lease_dict(session),
        "root_branch_id": session.root_branch_id,
        "created_by": session.created_by,
        "created_at": _iso(session.created_at),
        "ended_at": _iso(session.ended_at),
        "branches": [
            {"branch_id": b.branch_id, "status": b.status, "position": b.position,
             "parent_branch_id": b.parent_branch_id, "fork_index": b.fork_index}
            for b in branches
        ],
    }
    if include_inputs:
        out["inputs"] = session.inputs
    return out


def _branch_command_response(s, session: DebugSession, branch: DebugBranch) -> dict:
    return {"session_id": session.id, "lease": _lease_dict(session),
            "expected_seq": branch.next_seq, "branch": branch_dict(s, session, branch)}


# --------------------------------------------------------------------------- #
# 创建会话
# --------------------------------------------------------------------------- #

def create_session(s, *, policy: str, inputs: dict, actor: str,
                   version: int | None = None, title: str = "",
                   breakpoints: list | None = None,
                   secret_keys: list[str] | None = None,
                   lease_ttl_s: int | None = None) -> dict:
    pol = s.query(Policy).filter_by(name=policy).first()
    if not pol:
        raise DebugError(404, "policy_not_found", f"policy '{policy}' not found")

    if version is not None:
        art = (s.query(Artifact)
               .filter_by(policy_name=policy, version=version).first())
        if not art:
            raise DebugError(404, "artifact_not_found",
                             f"artifact {policy}@{version} not found")
    else:
        art = (s.query(Artifact).filter_by(policy_name=policy, revoked=False)
               .order_by(Artifact.version.desc()).first())
        if not art:
            raise DebugError(409, "no_available_version",
                             f"no non-revoked artifact for policy '{policy}'")

    bps = [_normalise_bp(bp) for bp in (breakpoints or [])]
    for bp in bps:
        validate_breakpoint(bp, art.topo)

    redacted, redaction_report = redact_inputs(inputs or {}, set(secret_keys or []))

    ttl = min(max(lease_ttl_s or DEBUG_DEFAULT_LEASE_S, 1), DEBUG_MAX_LEASE_S)
    token = secrets.token_hex(16)
    now = utcnow()

    session = DebugSession(
        title=title or "", policy_name=policy, status="active",
        pinned_version=art.version, pinned_hash=art.hash,
        nodes=art.nodes, topo=art.topo, entry_node=art.entry_node,
        dep_chain=art.dep_chain,
        inputs=redacted, input_redaction=redaction_report,
        lease_holder=actor, lease_token=token, lease_ttl_s=ttl,
        lease_expires_at=datetime_add(now, ttl),
        root_branch_id="main", created_by=actor,
    )
    s.add(session)
    s.flush()  # 取 session.id

    main = DebugBranch(session_id=session.id, branch_id="main",
                       inputs=redacted, position=0, status="paused",
                       breakpoints=bps, next_seq=1)
    s.add(main)
    _event(s, session.id, "session_created", actor,
           policy=policy, version=art.version, artifact_hash=art.hash,
           title=session.title, total_nodes=len(art.topo),
           input_summary=redaction_report["original_input_summary"],
           masked_paths=redaction_report["masked_paths"],
           lease_ttl_s=ttl)
    s.commit()

    out = session_dict(s, session)
    out["lease"]["token"] = token  # token 仅在创建/接管响应中返回
    return out


def datetime_add(dt, seconds: int):
    from datetime import timedelta
    return dt + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# 租约：续租 / 接管
# --------------------------------------------------------------------------- #

def renew_lease(s, session_id: int, actor: str, token: str,
                ttl_s: int | None = None) -> dict:
    session = _get_session(s, session_id)
    _active(session)
    if actor != session.lease_holder or token != session.lease_token:
        # 租约未到期时持有者身份不符：不能抢；到期后应走 takeover
        if _lease_active(session):
            raise DebugError(409, "not_lease_holder",
                             f"lease belongs to '{session.lease_holder}'",
                             lease_holder=session.lease_holder)
        raise DebugError(409, "lease_expired",
                         "lease expired; call /lease with takeover=true",
                         lease_holder=session.lease_holder)
    if not _lease_active(session):
        raise DebugError(409, "lease_expired",
                         "lease already expired; call /lease with takeover=true",
                         lease_holder=session.lease_holder)
    ttl = min(max(ttl_s or session.lease_ttl_s, 1), DEBUG_MAX_LEASE_S)
    session.lease_ttl_s = ttl
    session.lease_expires_at = datetime_add(utcnow(), ttl)
    _event(s, session.id, "lease_renewed", actor, expires_at=_iso(session.lease_expires_at))
    s.commit()
    return {"session_id": session.id, "lease": _lease_dict(session)}


def take_lease(s, session_id: int, actor: str, ttl_s: int | None = None) -> dict:
    """接管：仅当当前租约到期。同一持有人到期重新获得也走这里（换新 token）。"""
    session = _get_session(s, session_id)
    _active(session)
    if _lease_active(session):
        raise DebugError(409, "lease_active",
                         f"lease is actively held by '{session.lease_holder}' "
                         "until expiry; takeover only after expiry",
                         lease_holder=session.lease_holder,
                         expires_at=_iso(session.lease_expires_at))
    old_holder = session.lease_holder
    ttl = min(max(ttl_s or session.lease_ttl_s, 1), DEBUG_MAX_LEASE_S)
    token = secrets.token_hex(16)
    session.lease_holder = actor
    session.lease_token = token
    session.lease_ttl_s = ttl
    session.lease_expires_at = datetime_add(utcnow(), ttl)
    _event(s, session.id, "lease_taken_over", actor,
           from_holder=old_holder, to_holder=actor,
           expires_at=_iso(session.lease_expires_at))
    s.commit()
    out = {"session_id": session.id, "lease": _lease_dict(session)}
    out["lease"]["token"] = token
    return out


# --------------------------------------------------------------------------- #
# 推进：step（同步一步）/ continue（后台到断点）/ pause
# --------------------------------------------------------------------------- #

def step(s, session_id: int, *, actor: str, token: str,
         branch_id: str | None = None, seq: int | None = None,
         cmd_id: str | None = None) -> dict:
    session = _get_session(s, session_id)
    _require_lease(session, actor, token)
    branch = _get_branch(s, session, branch_id)

    # 幂等回放优先：重复命令即使遇到后续状态校验也不能报错/推进
    _cmd, dup = _dedupe_command(s, session, cmd_id, "step", actor, branch.branch_id)
    if dup:
        s.commit()
        return {**_cmd.response, "replayed": True,
                "expected_seq": branch.next_seq}

    _no_running_branch(s, session)
    if branch.status == "running":
        raise DebugError(409, "branch_running", "branch is running; pause first")
    if branch.status in ("error", "completed"):
        raise DebugError(409, "branch_finished",
                         f"branch is {branch.status}; fork a new branch to continue",
                         branch_status=branch.status, error_frame=branch.error,
                         expected_seq=branch.next_seq)
    if seq is not None and seq != branch.next_seq:
        raise DebugError(409, "unexpected_seq",
                         f"expected seq {branch.next_seq}, got {seq}",
                         expected_seq=branch.next_seq, got_seq=seq)
    idx = branch.position
    nid = session.topo[idx]
    values = _load_values(s, session, branch)
    import time
    t0 = time.monotonic()
    try:
        args_in, value = _execute_node(session.nodes, nid, branch.inputs, values)
        error = None
    except dsl.DSLError as e:
        args_in, value, error = [], None, _classify_error(e, nid)
    duration_ms = round((time.monotonic() - t0) * 1000, 3)

    frame = DebugFrame(session_id=session.id, branch_id=branch.branch_id,
                       index=idx, node_id=nid, args_in=args_in,
                       result=None if error else value, error=error,
                       duration_ms=duration_ms)
    s.add(frame)
    s.flush()

    if error:
        # 确定性错误：游标停在出错节点（不前进），形成错误帧；会话保留
        branch.status = "error"
        branch.error = error
        _event(s, session.id, "error", actor, branch_id=branch.branch_id,
               index=idx, node_id=nid, error=error, args_in=args_in,
               duration_ms=duration_ms)
    else:
        values[nid] = value
        branch.position = idx + 1
        _event(s, session.id, "advanced", actor, branch_id=branch.branch_id,
               from_index=idx, to_index=idx + 1, node_id=nid, result=value,
               duration_ms=duration_ms)
        if idx + 1 >= len(session.topo):
            branch.status = "completed"
            branch.final_result = values.get(session.entry_node)
            _event(s, session.id, "completed", actor, branch_id=branch.branch_id,
                   last_index=idx, result=branch.final_result)
        else:
            branch.status = "paused"  # step 永远执行后暂停
    branch.next_seq += 1

    resp = _branch_command_response(s, session, branch)
    if _cmd is not None:
        _cmd.seq = seq
        _cmd.response = resp
    s.commit()
    return resp


def continue_session(s, session_id: int, *, actor: str, token: str,
                     branch_id: str | None = None, cmd_id: str | None = None) -> dict:
    """进入 running 并由后台线程推进；立即返回当前状态（202 语义由 HTTP 层表达）。"""
    session = _get_session(s, session_id)
    _require_lease(session, actor, token)
    branch = _get_branch(s, session, branch_id)

    _cmd, dup = _dedupe_command(s, session, cmd_id, "continue", actor, branch.branch_id)
    if dup:
        s.commit()
        return {**_cmd.response, "replayed": True}

    _no_running_branch(s, session)
    if branch.status == "running":
        raise DebugError(409, "branch_running", "already running")
    if branch.status in ("error", "completed"):
        raise DebugError(409, "branch_finished",
                         f"branch is {branch.status}; fork a new branch to continue",
                         branch_status=branch.status, error_frame=branch.error)

    branch.status = "running"
    branch.pause_requested = False
    branch.run_epoch = _PROCESS_EPOCH
    _event(s, session.id, "resumed", actor, branch_id=branch.branch_id,
           from_index=branch.position, run_epoch=_PROCESS_EPOCH)
    resp = _branch_command_response(s, session, branch)
    if _cmd is not None:
        _cmd.response = resp
    s.commit()

    # 后台推进：租约 token 快照，节点边界感知接管/暂停请求
    threading.Thread(
        target=_continue_worker,
        args=(session.id, branch.branch_id, actor, token, cmd_id),
        daemon=True, name=f"debug-continue-{session.id}-{branch.branch_id}",
    ).start()
    return resp


def _worker_lease_valid(session: DebugSession, actor: str, token: str) -> bool:
    """后台推进的租约校验：允许持有人续租（token 不变、到期时间延长），
    被接管（token 变化）或到期立即失效。"""
    return (session.lease_holder == actor
            and session.lease_token == token
            and _lease_active(session))


def _stop_running(s, session: DebugSession, branch: DebugBranch, actor: str,
                  reason: str, *, event_actor=None, **payload) -> dict:
    branch.status = "paused"
    branch.pause_requested = False
    _event(s, session.id, "paused", event_actor or actor, branch_id=branch.branch_id,
           index=branch.position, node_id=(session.topo[branch.position]
                                           if branch.position < len(session.topo)
                                           else None),
           reason=reason, **payload)
    resp = _branch_command_response(s, session, branch)
    return resp


def _continue_worker(session_id: int, branch_id: str, actor: str,
                     token: str, cmd_id: str | None) -> None:
    """节点边界循环：每节点前查 接管/暂停请求/断点；每节点提交一次（崩溃只丢当前节点）。"""
    import time
    try:
        while True:
            with SessionLocal() as s:
                session = _get_session(s, session_id)
                branch = _get_branch(s, session, branch_id)
                # 每节点边界续租本进程心跳：存活推进不会被其它实例的恢复误回收
                heartbeat(s)

                if branch.status != "running":
                    return  # 已被其他路径（end / 重启恢复）改状态
                if not _worker_lease_valid(session, actor, token):
                    reason = ("session ended" if session.status == "ended"
                              else "lease_lost")
                    resp = _stop_running(s, session, branch, actor, reason)
                    _finish_worker_command(s, session_id, cmd_id, resp)
                    s.commit()
                    return
                if branch.pause_requested:
                    resp = _stop_running(s, session, branch, actor, "manual",
                                         event_actor=session.lease_holder)
                    _finish_worker_command(s, session_id, cmd_id, resp)
                    s.commit()
                    return

                idx = branch.position
                if idx >= len(session.topo):
                    branch.status = "completed"
                    branch.final_result = _load_values(s, session, branch).get(
                        session.entry_node)
                    _event(s, session.id, "completed", actor,
                           branch_id=branch.branch_id, last_index=idx - 1,
                           result=branch.final_result)
                    resp = _branch_command_response(s, session, branch)
                    _finish_worker_command(s, session_id, cmd_id, resp)
                    s.commit()
                    return

                nid = session.topo[idx]
                node = session.nodes[nid]

                # 断点：执行前暂停
                hit = None
                for bp in branch.breakpoints:
                    matched, why = _bp_matches(bp, session, branch, nid, node)
                    if matched:
                        hit = {"breakpoint": bp, "detail": why}
                        break
                if hit:
                    resp = _stop_running(s, session, branch, actor, "breakpoint",
                                         event_actor=actor, **hit)
                    _finish_worker_command(s, session_id, cmd_id, resp)
                    s.commit()
                    return

                values = _load_values(s, session, branch)
                t0 = time.monotonic()
                try:
                    args_in, value = _execute_node(session.nodes, nid,
                                                   branch.inputs, values)
                    error = None
                except dsl.DSLError as e:
                    args_in, value, error = [], None, _classify_error(e, nid)
                duration_ms = round((time.monotonic() - t0) * 1000, 3)

                s.add(DebugFrame(session_id=session.id, branch_id=branch.branch_id,
                                 index=idx, node_id=nid, args_in=args_in,
                                 result=None if error else value, error=error,
                                 duration_ms=duration_ms))

                if error:
                    branch.status = "error"
                    branch.error = error
                    _event(s, session.id, "error", actor, branch_id=branch.branch_id,
                           index=idx, node_id=nid, error=error, args_in=args_in,
                           duration_ms=duration_ms)
                    resp = _branch_command_response(s, session, branch)
                    _finish_worker_command(s, session_id, cmd_id, resp)
                    s.commit()
                    return

                values[nid] = value
                branch.position = idx + 1
                _event(s, session.id, "advanced", actor, branch_id=branch.branch_id,
                       from_index=idx, to_index=idx + 1, node_id=nid, result=value,
                       duration_ms=duration_ms)
                if idx + 1 >= len(session.topo):
                    branch.status = "completed"
                    branch.final_result = values.get(session.entry_node)
                    _event(s, session.id, "completed", actor,
                           branch_id=branch.branch_id, last_index=idx,
                           result=branch.final_result)
                s.commit()  # 每节点落盘：长暂停/崩溃后从下一节点继续
    except Exception as e:  # noqa: BLE001 - 后台线程异常不能无声丢失会话
        with SessionLocal() as s:
            try:
                session = _get_session(s, session_id)
                branch = _get_branch(s, session, branch_id)
                if branch.status == "running":
                    resp = _stop_running(s, session, branch, actor,
                                         "internal_error", error=str(e))
                    _finish_worker_command(s, session_id, cmd_id, resp)
                s.commit()
            except Exception:  # noqa: BLE001
                pass


def _finish_worker_command(s, session_id: int, cmd_id: str | None, resp: dict) -> None:
    if not cmd_id:
        return
    row = (s.query(DebugCommand).filter_by(session_id=session_id, cmd_id=cmd_id).first())
    if row:
        row.response = resp


def request_pause(s, session_id: int, *, actor: str, token: str,
                  branch_id: str | None = None, cmd_id: str | None = None) -> dict:
    session = _get_session(s, session_id)
    _require_lease(session, actor, token)
    branch = _get_branch(s, session, branch_id)

    _cmd, dup = _dedupe_command(s, session, cmd_id, "pause", actor, branch.branch_id)
    if dup:
        s.commit()
        return {**_cmd.response, "replayed": True}

    if branch.status != "running":
        raise DebugError(409, "not_running",
                         f"branch is {branch.status}; nothing to pause",
                         branch_status=branch.status)
    if not branch.pause_requested:
        branch.pause_requested = True
        _event(s, session.id, "pause_requested", actor, branch_id=branch.branch_id,
               index=branch.position)
    resp = _branch_command_response(s, session, branch)
    if _cmd is not None:
        _cmd.response = resp
    s.commit()
    return resp


# --------------------------------------------------------------------------- #
# 断点管理 / 分叉 / 结束
# --------------------------------------------------------------------------- #

def set_breakpoints(s, session_id: int, *, actor: str, token: str,
                    breakpoints: list, branch_id: str | None = None,
                    cmd_id: str | None = None) -> dict:
    session = _get_session(s, session_id)
    _require_lease(session, actor, token)
    branch = _get_branch(s, session, branch_id)

    _cmd, dup = _dedupe_command(s, session, cmd_id, "set_breakpoints", actor,
                               branch.branch_id)
    if dup:
        s.commit()
        return {**_cmd.response, "replayed": True}

    normalised = [_normalise_bp(bp) for bp in breakpoints]
    for bp in normalised:
        validate_breakpoint(bp, session.topo)
    branch.breakpoints = normalised
    _event(s, session.id, "breakpoints_set", actor, branch_id=branch.branch_id,
           breakpoints=normalised)  # running 时下一节点边界生效
    resp = _branch_command_response(s, session, branch)
    if _cmd is not None:
        _cmd.response = resp
    s.commit()
    return resp


def _apply_patch(base: dict, patch: dict) -> set:
    """顶层合并：键存在即覆盖，值为 None 表示删除。返回改动的键集合。"""
    changed = set()
    for k, v in (patch or {}).items():
        changed.add(k)
        if v is None:
            base.pop(k, None)
        else:
            base[k] = v
    return changed


def fork_branch(s, session_id: int, *, actor: str, token: str,
                parent_branch_id: str | None = None, input_patch: dict | None = None,
                seq: int | None = None, cmd_id: str | None = None) -> dict:
    """从停止点分叉：父分支历史不动，子分支带修改后的输入从节点 0 重放。"""
    session = _get_session(s, session_id)
    _require_lease(session, actor, token)
    parent = _get_branch(s, session, parent_branch_id)

    _cmd, dup = _dedupe_command(s, session, cmd_id, "fork", actor, parent.branch_id)
    if dup:
        s.commit()
        return {**_cmd.response, "replayed": True}

    _no_running_branch(s, session)
    if parent.status == "running":
        raise DebugError(409, "branch_running",
                         "cannot fork from a running branch; pause it first")
    if seq is not None and seq != parent.next_seq:
        raise DebugError(409, "unexpected_seq",
                         f"expected seq {parent.next_seq}, got {seq}",
                         expected_seq=parent.next_seq, got_seq=seq)
    if not isinstance(input_patch or {}, dict):
        raise DebugError(422, "invalid_input_patch",
                         "input_patch must be an object of top-level input overrides; "
                         "null deletes a key")

    child_inputs = copy.deepcopy(parent.inputs)
    changed = _apply_patch(child_inputs, input_patch or {})
    # 新增/覆盖的值也要脱敏（沿用会话的敏感键集合）
    secret_keys = set(session.input_redaction.get("secret_keys", []))
    child_inputs, child_redaction = redact_inputs(child_inputs, secret_keys)

    existing_ids = {b.branch_id for b in
                    s.query(DebugBranch).filter_by(session_id=session.id).all()}
    while True:
        child_id = "b-" + secrets.token_hex(4)
        if child_id not in existing_ids:
            break

    child = DebugBranch(
        session_id=session.id, branch_id=child_id,
        parent_branch_id=parent.branch_id, fork_index=parent.position,
        fork_patch=input_patch or {},
        inputs=child_inputs, position=0, status="paused",
        breakpoints=copy.deepcopy(parent.breakpoints), next_seq=1,
    )
    s.add(child)
    parent.next_seq += 1
    _event(s, session.id, "forked", actor, branch_id=child_id,
           parent_branch=parent.branch_id, at_index=parent.position,
           parent_status=parent.status, changed_keys=sorted(changed),
           patch=input_patch or {},
           newly_masked_paths=sorted(
               set(child_redaction["masked_paths"])
               - set(session.input_redaction.get("masked_paths", []))),
           breakpoints_copied=len(child.breakpoints))
    resp = {"session_id": session.id, "lease": _lease_dict(session),
            "expected_seq": 1,
            "parent": {"branch_id": parent.branch_id, "position": parent.position,
                       "next_seq": parent.next_seq},
            "branch": branch_dict(s, session, child)}
    if _cmd is not None:
        _cmd.response = resp
    s.commit()
    return resp


def end_session(s, session_id: int, *, actor: str, token: str,
                reason: str = "ended_by_actor", cmd_id: str | None = None) -> dict:
    session = _get_session(s, session_id)
    _require_lease(session, actor, token)

    _cmd, dup = _dedupe_command(s, session, cmd_id, "end", actor, None)
    if dup:
        s.commit()
        return {**_cmd.response, "replayed": True}

    _no_running_branch(s, session)
    session.status = "ended"
    session.end_reason = reason
    session.ended_at = utcnow()
    _event(s, session.id, "ended", actor, reason=reason)
    resp = {"session_id": session.id, "status": "ended", "end_reason": reason,
            "ended_at": _iso(session.ended_at)}
    if _cmd is not None:
        _cmd.response = resp
    s.commit()
    return resp


# --------------------------------------------------------------------------- #
# 查询：会话 / 分支 / 时间线 / 分支比较
# --------------------------------------------------------------------------- #

def get_session(s, session_id: int) -> dict:
    session = _get_session(s, session_id)
    return session_dict(s, session)


def list_sessions(s, policy: str | None = None, limit: int = 50) -> list:
    limit = min(max(limit, 1), 500)
    q = s.query(DebugSession).order_by(DebugSession.id.desc())
    if policy:
        q = q.filter_by(policy_name=policy)
    return [session_dict(s, x, include_inputs=False) for x in q.limit(limit).all()]


def get_branch(s, session_id: int, branch_id: str | None) -> dict:
    session = _get_session(s, session_id)
    branch = _get_branch(s, session, branch_id)
    return {"session_id": session.id, "lease": _lease_dict(session),
            "branch": branch_dict(s, session, branch, frames=True)}


def get_timeline(s, session_id: int) -> dict:
    session = _get_session(s, session_id)
    branches = (s.query(DebugBranch).filter_by(session_id=session.id)
                .order_by(DebugBranch.id).all())
    events = (s.query(DebugEvent).filter_by(session_id=session.id)
              .order_by(DebugEvent.id).all())
    return {
        "session": session_dict(s, session),
        "branches": [branch_dict(s, session, b, frames=True) for b in branches],
        "events": [
            {"id": e.id, "ts": _iso(e.ts), "event_type": e.event_type,
             "actor": e.actor, "branch_id": e.branch_id, "payload": e.payload}
            for e in events
        ],
    }


def _lineage(branches: dict, bid: str):
    """返回 root -> bid 的支系：[(branch_id, 从父分叉时的 position)]，root 为 0。"""
    chain = []
    cur = bid
    while cur:
        b = branches[cur]
        chain.append((cur, b.fork_index or 0))
        cur = b.parent_branch_id
    chain.reverse()
    return chain


def _lca(branches: dict, a_id: str, b_id: str) -> str:
    """两个分支最近的共同祖先分支 id。"""
    seen = {bid for bid, _ in _lineage(branches, a_id)}
    for bid, _ in reversed(_lineage(branches, b_id)):
        if bid in seen:
            return bid
    raise DebugError(500, "no_common_ancestor", "branches share no ancestor")


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def compare_branches(s, session_id: int, a_id: str | None,
                     b_id: str | None) -> dict:
    """逐节点比较两个分支。

    所有分支执行同一固定产物的同一条 topo，帧的 topo 下标就是天然对齐键：
    从下标 0 起逐节点比较 结果 / 错误 / 是否已执行，找到第一处分歧即停。
    """
    session = _get_session(s, session_id)
    a = _get_branch(s, session, a_id)
    b = _get_branch(s, session, b_id)

    branches = {x.branch_id: x for x in
                s.query(DebugBranch).filter_by(session_id=session.id).all()}
    ancestor = _lca(branches, a.branch_id, b.branch_id)
    rel_a = "self" if a.branch_id == ancestor else "descendant"
    rel_b = "self" if b.branch_id == ancestor else "descendant"

    def frames_map(branch):
        return {f.index: f for f in
                s.query(DebugFrame).filter_by(session_id=session.id,
                                              branch_id=branch.branch_id)
                .order_by(DebugFrame.index).all()}

    fa, fb = frames_map(a), frames_map(b)

    divergence = None
    compared = 0
    for i in range(len(session.topo)):
        fra, frb = fa.get(i), fb.get(i)
        if fra is None and frb is None:
            break
        compared += 1
        if fra is None or frb is None:
            divergence = {
                "kind": "execution_boundary",
                "index": i, "node_id": session.topo[i],
                "a": frame_dict(fra) if fra else None,
                "b": frame_dict(frb) if frb else None,
                "detail": "one branch has not executed this node "
                          "(stopped at an earlier pause/error)",
            }
            break
        if (fra.error is None) != (frb.error is None):
            divergence = {
                "kind": "error", "index": i, "node_id": session.topo[i],
                "a": frame_dict(fra), "b": frame_dict(frb),
                "detail": "error frame occurs on exactly one branch",
            }
            break
        if _canon(fra.error) != _canon(frb.error) or _canon(fra.result) != _canon(frb.result):
            divergence = {
                "kind": "result", "index": i, "node_id": session.topo[i],
                "a": frame_dict(fra), "b": frame_dict(frb),
                "detail": "first node with a different intermediate result",
            }
            break

    def side(branch):
        cur = branch_dict(s, session, branch)
        return {"branch_id": branch.branch_id, "status": branch.status,
                "position": branch.position, "error": branch.error,
                "final_result": branch.final_result,
                "remaining_path": cur["remaining_path"],
                "current_node": (cur["current"] or {}).get("node_id"),
                "forked_from": branch.parent_branch_id,
                "forked_at_index": branch.fork_index}

    return {
        "session_id": session.id,
        "common_ancestor": ancestor,
        "relation": {"a": rel_a, "b": rel_b},
        "nodes_compared": compared,
        "equal": divergence is None,
        "first_divergence": divergence,
        "a": side(a), "b": side(b),
    }


# --------------------------------------------------------------------------- #
# 重启恢复（基于进程心跳，兼容同进程多个 lifespan 与同库多实例）
# --------------------------------------------------------------------------- #

# 心跳超过该秒数未刷新，判定推进它的进程已死亡（需大于单节点最大耗时）。
RECOVER_STALE_S = 10

# 本进程/lifespan 的启动序号（DebugEpoch.id）。
_PROCESS_EPOCH: int | None = None


def process_epoch() -> int | None:
    return _PROCESS_EPOCH


def bootstrap_epoch(s, *, service: str = "debugger"):
    """lifespan 启动：插入带心跳的进程序号，并恢复心跳已死的 running 分支。

    返回 (epoch, recovered_count)。
    """
    global _PROCESS_EPOCH
    row = DebugEpoch(service=service, heartbeat_at=utcnow())
    s.add(row)
    s.flush()
    _PROCESS_EPOCH = row.id
    recovered = recover_running(s)
    return _PROCESS_EPOCH, recovered


def heartbeat(s, epoch: int | None = None) -> None:
    """后台推进线程在节点边界刷新本进程心跳。"""
    epoch = _PROCESS_EPOCH if epoch is None else epoch
    if epoch is None:
        return
    row = s.get(DebugEpoch, epoch)
    if row is not None:
        row.heartbeat_at = utcnow()


def _epoch_alive(s, epoch, *, now=None) -> bool:
    """进程是否存活：心跳行存在且未过期（SQLite naive datetime 按 UTC）。"""
    from datetime import timedelta, timezone
    if epoch is None:
        return False
    row = s.get(DebugEpoch, epoch)
    if row is None or row.heartbeat_at is None:
        return False
    hb = row.heartbeat_at
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    now = now or utcnow()
    return hb >= now - timedelta(seconds=RECOVER_STALE_S)


def recover_running(s, *, actor: str = "debugger",
                    epoch: int | None = None) -> int:
    """把「所属进程已死」（心跳缺失/过期）的 running 分支复位为 paused。

    游标停在已落盘的下一节点；同库其它存活实例（含同进程另一 lifespan）
    心跳新鲜，其正在推进的分支不会被误复位。run_epoch 为空的极旧数据，
    无心跳可查，保守视为残留一并恢复。
    """
    current_epoch = epoch if epoch is not None else _PROCESS_EPOCH
    now = utcnow()
    rows = []
    for b in s.query(DebugBranch).filter_by(status="running").all():
        # 绝不恢复自己正在推进的分支
        if current_epoch is not None and b.run_epoch == current_epoch:
            continue
        if b.run_epoch is None or not _epoch_alive(s, b.run_epoch, now=now):
            rows.append(b)
    for b in rows:
        b.status = "paused"
        b.pause_requested = False
        _event(s, b.session_id, "paused", actor, branch_id=b.branch_id,
               index=b.position, reason="service_restart",
               recovered_from_epoch=b.run_epoch,
               recovered_by_epoch=current_epoch)
    if rows:
        s.commit()
    return len(rows)
