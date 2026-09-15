from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey,
                        Integer, String, UniqueConstraint)

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
