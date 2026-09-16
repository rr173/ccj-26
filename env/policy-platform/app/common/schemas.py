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
