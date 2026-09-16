from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class FragmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    body: Any
    actor: str = "anonymous"


class FragmentUpdate(BaseModel):
    body: Any
    actor: str = "anonymous"


class PolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    entry_fragment: str
    description: str = ""
    actor: str = "anonymous"


class PublishRequest(BaseModel):
    actor: str = "anonymous"


class CompileRequest(BaseModel):
    policy: str
    actor: str = "anonymous"


class AffectedRequest(BaseModel):
    fragment: str
    actor: str = "anonymous"


class BatchCompileRequest(BaseModel):
    policies: List[str]
    actor: str = "anonymous"


class RevokeRequest(BaseModel):
    reason: str = ""
    actor: str = "anonymous"


class QueryRequest(BaseModel):
    policy: str
    min_version: int = 0
    inputs: dict = Field(default_factory=dict)
    timeout_ms: Optional[int] = None
    request_id: Optional[str] = None
    strict_min_version: bool = False


# ---------- 变更管控：提案 / 评审 ----------

class ProposalChangeIn(BaseModel):
    fragment: str = Field(min_length=1, max_length=128)
    body: Any


class ProposalCreate(BaseModel):
    title: str = ""
    actor: str = "anonymous"
    changes: List[ProposalChangeIn] = Field(min_length=1)
    scheduled_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class ReviewCreate(BaseModel):
    actor: str = Field(min_length=1)
    role: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern="^(approved|rejected)$")
    comment: str = ""


class WithdrawRequest(BaseModel):
    actor: str = Field(min_length=1)


class ApprovalRule(BaseModel):
    role: str = Field(min_length=1, max_length=128)
    count: int = Field(ge=1)


class ApprovalConfigUpsert(BaseModel):
    rules: List[ApprovalRule] = Field(min_length=1)
    actor: str = "anonymous"


# ---------- 逐步调试会话 ----------

class DebugSessionCreate(BaseModel):
    policy: str
    inputs: dict = Field(default_factory=dict)
    actor: str = Field(min_length=1)
    version: Optional[int] = None              # 默认取最新未撤销版本
    title: str = ""
    breakpoints: List[dict] = Field(default_factory=list)
    secret_keys: List[str] = Field(default_factory=list)  # 额外脱敏键名
    lease_ttl_s: Optional[int] = Field(default=None, ge=1)


class DebugStepRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: str = Field(min_length=1)
    branch: Optional[str] = None
    seq: Optional[int] = None
    cmd_id: Optional[str] = None


class DebugContinueRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: str = Field(min_length=1)
    branch: Optional[str] = None
    cmd_id: Optional[str] = None


class DebugPauseRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: str = Field(min_length=1)
    branch: Optional[str] = None
    cmd_id: Optional[str] = None


class DebugForkRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: str = Field(min_length=1)
    parent_branch: Optional[str] = None
    input_patch: dict = Field(default_factory=dict)
    seq: Optional[int] = None
    cmd_id: Optional[str] = None


class DebugBreakpointsRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: str = Field(min_length=1)
    branch: Optional[str] = None
    breakpoints: List[dict] = Field(default_factory=list)
    cmd_id: Optional[str] = None


class DebugEndRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: str = Field(min_length=1)
    reason: str = "ended_by_actor"
    cmd_id: Optional[str] = None


class DebugLeaseRequest(BaseModel):
    actor: str = Field(min_length=1)
    token: Optional[str] = None
    ttl_s: Optional[int] = Field(default=None, ge=1)


class DebugLeaseTakeoverRequest(BaseModel):
    actor: str = Field(min_length=1)
    ttl_s: Optional[int] = Field(default=None, ge=1)


# ---------- 资源消耗台账与周期限额（quota :8005） ----------

class QuotaAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    timezone: str = "UTC"
    actor: str = "anonymous"


class QuotaRuleRegister(BaseModel):
    account: str = Field(min_length=1)
    rule_name: str = Field(min_length=1, max_length=128)
    mode: str = "dedicated"                # shared（共享池）| dedicated（独立设限）
    initial_limit: Optional[int] = Field(default=None, ge=0)
    actor: str = "anonymous"


class QuotaLimitSet(BaseModel):
    account: str = Field(min_length=1)
    # scope_rule 为空串/省略 = 账户共享池；否则为 dedicated 规则名
    scope_rule: str = ""
    amount: int = Field(ge=0)
    # 选定批次（含当日）起生效；None = 账户时区今天。已封账批次会 409
    effective_from: Optional[str] = None
    note: str = ""
    actor: str = "anonymous"


class QuotaHoldSubmit(BaseModel):
    serial: str = Field(min_length=1, max_length=128)
    account: str = Field(min_length=1)
    rule_name: str = Field(min_length=1, max_length=128)
    occurred_at: datetime                  # 判定开始时刻（带时区）
    amount: int = Field(ge=0)              # 预估消耗量（先占余额）
    ttl_s: Optional[int] = Field(default=None, ge=1)


class QuotaVoucherSubmit(BaseModel):
    serial: str = Field(min_length=1, max_length=128)
    account: str = Field(min_length=1)
    rule_name: str = Field(min_length=1, max_length=128)
    occurred_at: datetime                  # 真实发生时刻（带时区）
    amount: int = Field(ge=0)              # 真实消耗量
    kind: str = "consume"                  # consume | reverse


class QuotaAbortSubmit(BaseModel):
    serial: str = Field(min_length=1, max_length=128)
    account: str = Field(min_length=1)
    rule_name: str = ""
    occurred_at: datetime
    reason: str = "abort"


class QuotaSealRequest(BaseModel):
    account: str = Field(min_length=1)
    batch_date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    actor: str = "accountant"


class QuotaAdjustmentDecision(BaseModel):
    decision: str                           # confirm | reject
    actor: str = "finance"
    # 确认时可由财务修正金额（带符号：正补账/负冲账）；None = 按凭证原始方向金额
    amount: Optional[int] = None
    note: str = ""


class QuotaManualAdjustment(BaseModel):
    account: str = Field(min_length=1)
    scope_rule: str = ""
    batch_date: str = Field(min_length=10, max_length=10)
    amount: int                             # 非零有符号：正补账 / 负冲账
    serial: str = Field(min_length=1, max_length=128)
    note: str = ""
    actor: str = "finance"
