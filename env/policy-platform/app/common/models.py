from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Float,
                        ForeignKey, Index, Integer, String, UniqueConstraint)

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Fragment(Base):
    """可复用策略片段。body 变更即 version+1，旧版本内容仍被历史产物引用（通过 content_hash 固化在产物里）。"""

    __tablename__ = "fragments"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, index=True, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    body = Column(JSON, nullable=False)
    refs = Column(JSON, nullable=False, default=list)  # 从 body 提取的依赖片段名
    content_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by = Column(String(128), default="anonymous")


class Policy(Base):
    __tablename__ = "policies"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, index=True, nullable=False)
    entry_fragment = Column(String(128), nullable=False)
    description = Column(String(512), default="")
    latest_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    """不可变编译产物。编译失败绝不写表；撤销只是打标记，不删除。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("policy_name", "version", name="uq_artifact_policy_version"),
        UniqueConstraint("policy_name", "hash", name="uq_artifact_policy_hash"),
    )

    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, ForeignKey("policies.id"), nullable=False)
    policy_name = Column(String(128), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    hash = Column(String(64), nullable=False)  # 内容寻址哈希，用于重复发布去重
    nodes = Column(JSON, nullable=False)       # 节点表: id -> node
    entry_node = Column(String(256), nullable=False)
    topo = Column(JSON, nullable=False)        # 拓扑序节点 id 列表
    dep_chain = Column(JSON, nullable=False)   # [{fragment, version, hash}] 按拓扑序
    created_at = Column(DateTime(timezone=True), default=utcnow)
    revoked = Column(Boolean, nullable=False, default=False)
    revoke_reason = Column(String(512))
    revoked_by = Column(String(128))
    revoked_at = Column(DateTime(timezone=True))


class PolicyDep(Base):
    """反向依赖索引：某片段更新时，只重编译依赖闭包中包含它的策略。"""

    __tablename__ = "policy_deps"

    policy_name = Column(String(128), primary_key=True)
    fragment_name = Column(String(128), primary_key=True, index=True)


class AuditEvent(Base):
    """追加式审计日志：发布/失败/重复/撤销/重启等状态变更全部留痕。"""

    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), default=utcnow, index=True)
    service = Column(String(64), nullable=False)
    event_type = Column(String(64), nullable=False, index=True)
    actor = Column(String(128), default="anonymous")
    payload = Column(JSON, nullable=False, default=dict)


class Decision(Base):
    """每次查询决策：用了哪版、是否回退、输入摘要、依赖链快照。"""

    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), default=utcnow, index=True)
    request_id = Column(String(128), index=True)
    policy = Column(String(128), index=True, nullable=False)
    requested_min_version = Column(Integer, nullable=False)
    used_version = Column(Integer)              # NULL 表示所有候选版本都失败
    below_min_version = Column(Boolean, default=False)
    fallback_from = Column(Integer)
    fallback_reason = Column(JSON)              # [{version, reason, ...}]
    input_summary = Column(JSON, nullable=False)
    result = Column(JSON)
    error = Column(String(1024))
    latency_ms = Column(Float)
    dep_chain = Column(JSON)                    # 实际执行产物的依赖链快照


# ---------------------------------------------------------------------------
# 变更管控：提案 -> 评审 -> （立即/预约）生效
# ---------------------------------------------------------------------------

class ApprovalConfig(Base):
    """审批角色配置。policy_name 为空串 "" 表示全局默认；按策略可覆盖。

    rules: [{"role": "security", "count": 1}, {"role": "ops", "count": 2}]
    含义：提案生效前，列出的每个角色都必须至少有 count 名不同评审人同意。
    """

    __tablename__ = "approval_configs"

    # 空串 = 全局默认（SQL 主键不可为 NULL 的折中，API 层映射回 None）
    policy_name = Column(String(128), primary_key=True, nullable=False)
    rules = Column(JSON, nullable=False, default=list)
    updated_by = Column(String(128), default="anonymous")
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Proposal(Base):
    """变更提案：提交时固定每个变更项的基线版本/内容、影响范围与逐项语义差异。

    状态机：
      IN_REVIEW  等待评审（可带预约时间）
      APPROVED   评审通过且选择立即生效（apply 在同事务内尝试，正常很快变 EFFECTIVE）
      SCHEDULED  评审通过、等待 scheduled_at 由调度器生效
      EFFECTIVE  已应用，产物进入运行环境
      REJECTED / WITHDRAWN / EXPIRED / CONFLICT / APPLY_FAILED 终态
    """

    __tablename__ = "proposals"

    id = Column(Integer, primary_key=True)
    title = Column(String(256), nullable=False, default="")
    target_kind = Column(String(32), nullable=False, default="fragment")
    status = Column(String(32), nullable=False, default="IN_REVIEW", index=True)
    proposer = Column(String(128), nullable=False, index=True)

    # 变更项（提交即固化）：
    # [{fragment, action, base_version, base_hash, base_body,
    #   new_body, new_hash, diff}]
    changes = Column(JSON, nullable=False, default=list)
    # 影响范围（提交即固化）：受影响策略 + 评审时整个依赖闭包的片段版本/哈希快照
    # {"policies": [...],
    #  "fragments": {"name": {"version": n, "hash": "..."}},
    #  "policy_versions": {"policy": latest_version}}
    baseline = Column(JSON, nullable=False, default=dict)

    scheduled_at = Column(DateTime(timezone=True))   # 预约生效时间
    expires_at = Column(DateTime(timezone=True))     # 审批截止时间
    # 生效排队顺序（同策略多提案按 (scheduled_at, id) 确定顺序）
    seq = Column(BigInteger, index=True)

    # 生效结果：[{"policy", "version", "hash", "status"}] + 冲突说明
    applied = Column(JSON)
    conflict_reason = Column(JSON)

    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    decided_at = Column(DateTime(timezone=True))     # 评审达到终态（通过/拒绝/过期…）
    effective_at = Column(DateTime(timezone=True))  # 实际进入运行环境的时间

    __table_args__ = (
        Index("ix_proposals_policy_status", "status", "scheduled_at"),
    )


class Review(Base):
    """评审意见：(提案, 评审人) 唯一 —— 重复审批不会产生第二条记录。"""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("proposal_id", "reviewer", name="uq_review_proposal_reviewer"),
    )

    id = Column(Integer, primary_key=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False, index=True)
    reviewer = Column(String(128), nullable=False)
    role = Column(String(128), nullable=False)
    decision = Column(String(16), nullable=False)   # approved | rejected
    comment = Column(String(1024), default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)


class ProposalEvent(Base):
    """提案时间线（追加式）：提交/评审决定/撤回/过期/冲突/预约/生效全程留痕。"""

    __tablename__ = "proposal_events"

    id = Column(Integer, primary_key=True)
    proposal_id = Column(Integer, ForeignKey("proposals.id"), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), default=utcnow, index=True)
    event_type = Column(String(48), nullable=False)
    actor = Column(String(128), default="anonymous")
    payload = Column(JSON, nullable=False, default=dict)


# ---------------------------------------------------------------------------
# 逐步调试会话：固定产物版本与脱敏输入，支持断点 / 租约 / 分叉 / 错误帧
#
# 设计要点：
# - 会话创建时把产物节点图（nodes/topo/entry/dep_chain/hash）整体快照进会话行，
#   之后即使产物被撤销或出现新版本，调试仍走固定版本（版本状态实时标注）。
# - 历史不可变：DebugFrame 对 (session, branch, index) 唯一，只追加；
#   分叉只新增 DebugBranch，永不改写父分支的帧。
# - 所有推进/暂停/接管/分叉/结束/恢复都追加 DebugEvent，时间线完整可查。
# - 命令带 cmd_id 去重 + 分支级 seq 防乱序，租约（holder/token/expires）
#   保证同一时刻只有持有人能推进。
# ---------------------------------------------------------------------------

class DebugSession(Base):
    """调试会话：一次输入 × 一个固定产物版本（快照）。"""

    __tablename__ = "debug_sessions"

    id = Column(Integer, primary_key=True)
    title = Column(String(256), default="")
    policy_name = Column(String(128), index=True, nullable=False)
    status = Column(String(16), nullable=False, default="active", index=True)  # active | ended
    end_reason = Column(String(256))

    # 产物版本固定（快照自创建时的 Artifact，调试期不再读 Artifact 的节点图）
    pinned_version = Column(Integer, nullable=False)
    pinned_hash = Column(String(64), nullable=False)
    nodes = Column(JSON, nullable=False)
    topo = Column(JSON, nullable=False)
    entry_node = Column(String(256), nullable=False)
    dep_chain = Column(JSON, nullable=False, default=list)

    # 脱敏后的输入 + 脱敏报告（原始输入不落库，仅存键名/哈希/大小）
    inputs = Column(JSON, nullable=False)
    input_redaction = Column(JSON, nullable=False, default=dict)

    # 租约：全局同一时刻只有 lease_holder 持 token 能推进
    lease_holder = Column(String(128), nullable=False)
    lease_token = Column(String(64))
    lease_expires_at = Column(DateTime(timezone=True))
    lease_ttl_s = Column(Integer, nullable=False, default=60)

    root_branch_id = Column(String(64), nullable=False, default="main")
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    ended_at = Column(DateTime(timezone=True))


class DebugBranch(Base):
    """调试分支：main 或从某次暂停点分叉出的子分支（各自独立输入/断点/命令序号）。"""

    __tablename__ = "debug_branches"
    __table_args__ = (
        UniqueConstraint("session_id", "branch_id", name="uq_debug_branch_id"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("debug_sessions.id"), nullable=False, index=True)
    branch_id = Column(String(64), nullable=False)

    # 分叉谱系（main 的 parent 为 NULL）
    parent_branch_id = Column(String(64))
    fork_index = Column(Integer)          # 父分支分叉时的 position
    fork_patch = Column(JSON)             # 相对父分支输入的修改

    # 该分支使用的（已脱敏）输入；position = 下一个待执行节点在 topo 中的下标
    inputs = Column(JSON, nullable=False)
    position = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="paused", index=True)
    # paused | running | error | completed
    error = Column(JSON)                   # 错误帧内容（status=error 时）
    final_result = Column(JSON)
    pause_requested = Column(Boolean, nullable=False, default=False)

    # 断点（创建时给定/分叉时继承，可整体替换）：
    # [{"type":"node","node":"f#3","enabled":true},
    #  {"type":"op","op":"div","enabled":true},
    #  {"type":"condition","expr":{...DSL...},"node":null,"enabled":true}]
    breakpoints = Column(JSON, nullable=False, default=list)

    # 分支级命令序号：下一条期望的 seq（乱序命令返回它）
    next_seq = Column(Integer, nullable=False, default=1)

    # running 时标记推进它的进程 epoch（重启恢复只回收旧 epoch 的残留分支）
    run_epoch = Column(Integer)

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DebugFrame(Base):
    """节点执行帧（只追加）：每个节点执行当时的入参、结果/错误、耗时。

    对 (session, branch, index) 唯一：分支历史一旦写下不可改写、不可重复。
    """

    __tablename__ = "debug_frames"
    __table_args__ = (
        UniqueConstraint("session_id", "branch_id", "index",
                         name="uq_debug_frame_branch_index"),
        Index("ix_debug_frames_session_branch", "session_id", "branch_id"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, nullable=False)
    branch_id = Column(String(64), nullable=False)
    index = Column(Integer, nullable=False)           # topo 下标
    node_id = Column(String(256), nullable=False)
    args_in = Column(JSON, nullable=False, default=list)
    result = Column(JSON)
    error = Column(JSON)                             # {"type":..., "message":...}
    duration_ms = Column(Float)
    ts = Column(DateTime(timezone=True), default=utcnow)


class DebugEvent(Base):
    """调试会话时间线（追加式）：推进/暂停/接管/分叉/结束/恢复全部留痕。"""

    __tablename__ = "debug_events"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("debug_sessions.id"), nullable=False, index=True)
    branch_id = Column(String(64), index=True)
    ts = Column(DateTime(timezone=True), default=utcnow, index=True)
    event_type = Column(String(48), nullable=False)
    actor = Column(String(128), default="anonymous")
    payload = Column(JSON, nullable=False, default=dict)


class DebugCommand(Base):
    """已接收的变更类命令：cmd_id 幂等去重（重复命令不重复推进）。"""

    __tablename__ = "debug_commands"
    __table_args__ = (
        UniqueConstraint("session_id", "cmd_id", name="uq_debug_command_id"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, nullable=False, index=True)
    branch_id = Column(String(64))
    cmd_id = Column(String(128), nullable=False)
    command = Column(String(32), nullable=False)
    actor = Column(String(128), nullable=False)
    seq = Column(Integer)
    request = Column(JSON, nullable=False, default=dict)
    response = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class DebugEpoch(Base):
    """调试进程启动 + 活动心跳。

    每个 lifespan 启动插入一行（取递增进程序号），后台推进线程在节点边界
    持续刷新 heartbeat_at。恢复时只回收「心跳缺失或过期」的 running 分支：
    存活实例（含同进程另一个 lifespan）的心跳是新鲜的，不会被误复位。
    """

    __tablename__ = "debug_epochs"

    id = Column(Integer, primary_key=True)
    service = Column(String(64), nullable=False, default="debugger")
    ts = Column(DateTime(timezone=True), default=utcnow)
    heartbeat_at = Column(DateTime(timezone=True))
