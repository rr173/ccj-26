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

# ---------------------------------------------------------------------------
# 资源消耗台账与周期限额（quota 服务 :8005）
#
# 一次规则判定的生命周期：
#   判定开始  -> 占用（hold，预估量先占余额）
#   判定结束  -> 凭证（voucher，真实消耗）到达 -> 销账（settle，差额自动补退）
#   异常/超时 -> 归还（release）回产生占用的那个周期
# 同一条业务流水 serial 串起：占用事件 -> 占用 -> 凭证 -> 结算/归还 -> 账页行/调整。
#
# 周期按「账户时区日」切分（batch_date = 发生时刻在账户时区下的日期）。
# 批次封账(SEALED)后整体快照成只读账页；晚到凭证不再进账页，先挂起，
# 财务确认后另记 adjustment_entry（补账 + / 冲账 -），原账页永不覆盖。
#
# 不可变表（settlements / releases / page_lines / pages / adjustment_entries /
# inbox_events 去重后）在 Postgres 上由 DDL 触发器拒绝 UPDATE/DELETE；
# SQLite 下由应用服务层只追加约定保证。
# ---------------------------------------------------------------------------

class QuotaAccount(Base):
    """限额账户。结算周期按 timezone 的本地日切（如 Asia/Shanghai 按 UTC+8 切日）。"""

    __tablename__ = "quota_accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, index=True, nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    created_by = Column(String(128), default="anonymous")


class QuotaRule(Base):
    """账户下的判定规则。

    mode = "shared"：并入账户共享池（scope_rule 固定为 SHARED_SCOPE=""），
                     多条规则共用同一份限额；
    mode = "dedicated"：以规则名为 scope 独立设限（限额版本独立）。
    """

    __tablename__ = "quota_rules"
    __table_args__ = (
        UniqueConstraint("account_id", "rule_name", name="uq_quota_rule"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("quota_accounts.id"), nullable=False, index=True)
    rule_name = Column(String(128), nullable=False)
    mode = Column(String(16), nullable=False, default="dedicated")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    created_by = Column(String(128), default="anonymous")


class QuotaVersion(Base):
    """限额版本：同一限额作用域（账户+scope_rule）的限额可变更。

    scope_rule = ""（SHARED_SCOPE）表示账户共享池；否则为某条独立设限规则。
    effective_from 为「选定批次」（账户时区日，YYYY-MM-DD）：新版本从该日批次
    起生效；该日之前（含已经封账）的批次不重新计算。同一作用域版本号单调递增。
    """

    __tablename__ = "quota_versions"
    __table_args__ = (
        UniqueConstraint("account_id", "scope_rule", "version",
                         name="uq_quota_version"),
        Index("ix_quota_version_scope", "account_id", "scope_rule", "effective_from"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("quota_accounts.id"), nullable=False)
    scope_rule = Column(String(128), nullable=False)   # "" = 共享池
    version = Column(Integer, nullable=False)
    limit_amount = Column(BigInteger, nullable=False)
    effective_from = Column(String(10), nullable=False)  # YYYY-MM-DD（含当日）
    note = Column(String(512), default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    created_by = Column(String(128), default="anonymous")


class QuotaBatch(Base):
    """结算批次：账户 × 账户时区日。当日批次 OPEN；封账后 SEALED 且不得再动账页。"""

    __tablename__ = "quota_batches"
    __table_args__ = (
        UniqueConstraint("account_id", "batch_date", name="uq_quota_batch"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("quota_accounts.id"), nullable=False, index=True)
    batch_date = Column(String(10), nullable=False)        # YYYY-MM-DD（账户时区）
    status = Column(String(8), nullable=False, default="OPEN", index=True)
    sealed_at = Column(DateTime(timezone=True))
    sealed_by = Column(String(128))
    created_at = Column(DateTime(timezone=True), default=utcnow)


class QuotaMeta(Base):
    """台账内部键值元数据（如占用通行令的 HMAC 签名密钥，按库持久化）。"""

    __tablename__ = "quota_meta"

    key = Column(String(64), primary_key=True)
    value = Column(String(256), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class QuotaInboxEvent(Base):
    """采集端入站事件（凭证 / 占用申请 / 放弃）。只追加。

    (serial, event_type) 唯一：重投（相同流水号+事件类型）直接判重复，绝不重复入账；
    凭证先到、占用后到等乱序场景由 gate/accountant 的处理函数容忍（查不到配对先
    挂回 NEW，后续周期重试）。claimed_by 为当前认领实例（崩溃恢复后可被重新认领）。
    """

    __tablename__ = "quota_inbox_events"
    __table_args__ = (
        UniqueConstraint("serial", "event_type", name="uq_quota_inbox_serial_type"),
        Index("ix_quota_inbox_status_type", "status", "event_type"),
    )

    id = Column(Integer, primary_key=True)
    serial = Column(String(128), nullable=False)
    event_type = Column(String(16), nullable=False)      # hold | voucher | abort
    account = Column(String(128), nullable=False)
    rule_name = Column(String(128), nullable=False, default="")
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    source = Column(String(128), default="http")        # http | spool | ...
    # NEW：待处理；PROCESSING：被某实例认领；DONE：已落地；FAILED：永久失败
    status = Column(String(16), nullable=False, default="NEW", index=True)
    claimed_by = Column(String(128))
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(1024))
    received_at = Column(DateTime(timezone=True), default=utcnow, index=True)
    processed_at = Column(DateTime(timezone=True))


class QuotaHold(Base):
    """判定开始时的余额占用（按预估量）。状态机：HELD -> SETTLED / RELEASED。

    batch_date 是占用产生的批次（开始时刻在账户时区的日期）；归还永远回到这个批次。
    reject_reason 非空表示门禁拒绝（余额不足/批次已封账…），此时不占任何余额，
    同 serial 的真实凭证若后来到达，仍按晚到凭证流程挂起等财务确认。
    """

    __tablename__ = "quota_holds"
    __table_args__ = (
        UniqueConstraint("serial", name="uq_quota_hold_serial"),
        Index("ix_quota_hold_state", "status", "expires_at"),
    )

    id = Column(Integer, primary_key=True)
    serial = Column(String(128), nullable=False)
    account_id = Column(Integer, ForeignKey("quota_accounts.id"), nullable=False, index=True)
    rule_name = Column(String(128), nullable=False, default="")
    scope_rule = Column(String(128), nullable=False)    # 作用域快照（""=共享池）
    amount = Column(BigInteger, nullable=False)         # 预估占用量
    batch_date = Column(String(10), nullable=False)     # 产生周期
    status = Column(String(16), nullable=False, default="HELD", index=True)
    # HELD | SETTLED | RELEASED | REJECTED
    reject_reason = Column(String(64))
    ttl_s = Column(Integer)
    started_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    created_by = Column(String(128), default="anonymous")


class QuotaVoucher(Base):
    """业务端提交的消耗凭证（真实消耗）。只追加。

    status:
      PENDING   等待配对/处理（先到于占用事件、或等待批次核算）
      SETTLED   已销账（真实消耗已入账，差额从占用补退）
      SUSPENDED 晚到凭证：所属批次已封账，挂起等待财务确认
      ADJUSTED  已由财务确认并另记调整（补账/冲账）
    """

    __tablename__ = "quota_vouchers"
    __table_args__ = (
        UniqueConstraint("serial", name="uq_quota_voucher_serial"),
        Index("ix_quota_voucher_status", "status"),
    )

    id = Column(Integer, primary_key=True)
    serial = Column(String(128), nullable=False)
    account_id = Column(Integer, ForeignKey("quota_accounts.id"), nullable=False, index=True)
    rule_name = Column(String(128), nullable=False, default="")
    scope_rule = Column(String(128), nullable=False, default="")
    amount = Column(BigInteger, nullable=False)         # 真实消耗量（>=0）
    kind = Column(String(16), nullable=False, default="consume")  # consume|reverse
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    batch_date = Column(String(10), nullable=False)     # 发生时刻所属批次
    status = Column(String(16), nullable=False, default="PENDING", index=True)
    suspend_reason = Column(String(64))
    received_at = Column(DateTime(timezone=True), default=utcnow)


class QuotaSettlement(Base):
    """销账记录（只追加）：同一 hold_serial 唯一 —— 同一流水多次销账只生效一次。"""

    __tablename__ = "quota_settlements"
    __table_args__ = (
        UniqueConstraint("hold_serial", name="uq_quota_settlement_hold"),
    )

    id = Column(Integer, primary_key=True)
    serial = Column(String(128), nullable=False, index=True)  # 凭证流水
    hold_serial = Column(String(128), nullable=False)
    account_id = Column(Integer, nullable=False, index=True)
    scope_rule = Column(String(128), nullable=False)
    held_amount = Column(BigInteger, nullable=False)
    actual_amount = Column(BigInteger, nullable=False)
    # 相对占用的净变化：正=追加占用（真实>预估），负=退回余额
    delta_amount = Column(BigInteger, nullable=False)
    # 账记在真实消耗发生的批次；与占用批次不同即跨周期销账
    batch_date = Column(String(10), nullable=False, index=True)
    origin_batch_date = Column(String(10), nullable=False)
    over_limit = Column(Boolean, nullable=False, default=False)
    ts = Column(DateTime(timezone=True), default=utcnow)


class QuotaRelease(Base):
    """占用归还记录（只追加）：异常退出 / 超时 / 主动放弃。

    归还永远落在 hold.origin_batch（产生占用的周期）。跨周期未结束的占用
    即使在更晚周期才被回收，也回到产生它的那一天。
    """

    __tablename__ = "quota_releases"
    __table_args__ = (
        UniqueConstraint("hold_serial", name="uq_quota_release_hold"),
    )

    id = Column(Integer, primary_key=True)
    serial = Column(String(128), nullable=False, index=True)
    hold_serial = Column(String(128), nullable=False)
    account_id = Column(Integer, nullable=False, index=True)
    scope_rule = Column(String(128), nullable=False)
    amount = Column(BigInteger, nullable=False)
    batch_date = Column(String(10), nullable=False)     # 归还目标批次=占用产生批次
    reason = Column(String(32), nullable=False)         # abort | timeout | sealed_late
    ts = Column(DateTime(timezone=True), default=utcnow)
    reaped_by = Column(String(128), default="gate")


class QuotaPage(Base):
    """封账账页（只追加、不可改）：批次封账时对当时账目的整体快照。"""

    __tablename__ = "quota_pages"
    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_quota_page_batch"),
    )

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey("quota_batches.id"), nullable=False)
    account_id = Column(Integer, nullable=False, index=True)
    batch_date = Column(String(10), nullable=False, index=True)
    # 快照：[{scope_rule, rule?, limit_version, limit_amount, settled, held_open,
    #        rejected, serials:{...}}]
    snapshot = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    created_by = Column(String(128), default="accountant")


class QuotaPageLine(Base):
    """账页明细行（只追加）：封账时刻每条结算/未结占用/拒绝在账页上固化的一行。"""

    __tablename__ = "quota_page_lines"
    __table_args__ = (
        UniqueConstraint("page_id", "line_no", name="uq_quota_page_line"),
        Index("ix_quota_page_line_scope", "page_id", "scope_rule"),
    )

    id = Column(Integer, primary_key=True)
    page_id = Column(Integer, ForeignKey("quota_pages.id"), nullable=False)
    line_no = Column(Integer, nullable=False)
    serial = Column(String(128), nullable=False, index=True)
    scope_rule = Column(String(128), nullable=False)
    line_type = Column(String(16), nullable=False)     # settlement | open_hold | rejected
    amount = Column(BigInteger, nullable=False)
    detail = Column(JSON, nullable=False, default=dict)


class QuotaAdjustment(Base):
    """调整单：晚到凭证先挂起（PENDING），财务确认后另记补账/冲账（CONFIRMED）。

    原账页（quota_pages / quota_page_lines）永不改写；调整结果写
    quota_adjustment_entries（只追加），并把原凭证置 ADJUSTED。
    冲账 amount 为负；财务可在确认时修正金额，但冲账后该作用域调整净额
    不得为负（不能冲掉不存在的消耗）。
    """

    __tablename__ = "quota_adjustments"
    __table_args__ = (
        UniqueConstraint("serial", "batch_date", name="uq_quota_adj_serial_batch"),
        Index("ix_quota_adj_scope_status", "account_id", "scope_rule", "status"),
    )

    id = Column(Integer, primary_key=True)
    serial = Column(String(128), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("quota_accounts.id"), nullable=False, index=True)
    scope_rule = Column(String(128), nullable=False)
    rule_name = Column(String(128), nullable=False, default="")
    batch_date = Column(String(10), nullable=False)
    voucher_amount = Column(BigInteger, nullable=False)
    requested_kind = Column(String(16), nullable=False, default="consume")
    # PENDING -> CONFIRMED / REJECTED
    status = Column(String(16), nullable=False, default="PENDING", index=True)
    reason = Column(String(64), nullable=False)        # late_voucher ...
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    decided_by = Column(String(128))
    decided_at = Column(DateTime(timezone=True))
    decision_note = Column(String(512), default="")


class QuotaAdjustmentEntry(Base):
    """已确认调整的入账分录（只追加）：补账 amount>0，冲账 amount<0。"""

    __tablename__ = "quota_adjustment_entries"
    __table_args__ = (
        UniqueConstraint("adjustment_id", name="uq_quota_adj_entry"),
    )

    id = Column(Integer, primary_key=True)
    adjustment_id = Column(Integer, ForeignKey("quota_adjustments.id"), nullable=False)
    serial = Column(String(128), nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    scope_rule = Column(String(128), nullable=False)
    batch_date = Column(String(10), nullable=False, index=True)
    amount = Column(BigInteger, nullable=False)       # 有符号：补+ / 冲-
    kind = Column(String(16), nullable=False)         # supplement | reversal
    limit_version = Column(Integer, nullable=False)   # 按该批次当时有效限额版本
    ts = Column(DateTime(timezone=True), default=utcnow)
    confirmed_by = Column(String(128), default="anonymous")
