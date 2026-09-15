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
