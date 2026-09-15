"""编译服务核心逻辑（HTTP 无关）：

- 依赖图解析 + 循环检测 + 不可变版本产物
- 内容哈希去重：相同内容重复发布不产生新版本（幂等）
- 并发安全：Postgres 行锁串行化同一策略的版本分配；唯一约束兜底；
  SQLite/冲突时按 (policy, version)/(policy, hash) 唯一约束重试
- 失败路径只写审计，绝不动已有产物
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError, OperationalError

from . import compiler_core, dsl, events
from .models import Artifact, Fragment, Policy, PolicyDep

MAX_ATTEMPTS = 3


def _fail(session, service, actor, policy_name, error, error_type=None):
    events.audit(session, service, "PUBLISH_FAILED", actor,
                 policy=policy_name, error=error, error_type=error_type)
    session.commit()
    return {"status": "failed", "policy": policy_name, "error": error}


def compile_policy(session, policy_name: str, actor: str = "anonymous",
                   service: str = "compiler") -> dict:
    events.audit(session, service, "PUBLISH_REQUESTED", actor, policy=policy_name)
    session.commit()

    policy = session.query(Policy).filter_by(name=policy_name).first()
    if policy is None:
        events.audit(session, service, "PUBLISH_FAILED", actor,
                     policy=policy_name, error="policy_not_found")
        session.commit()
        return {"status": "not_found", "policy": policy_name}

    fragments = {f.name: f for f in session.query(Fragment).all()}
    if policy.entry_fragment not in fragments:
        return _fail(session, service, actor, policy_name,
                     f"entry fragment '{policy.entry_fragment}' not found",
                     "MissingRefError")

    try:
        artifact = compiler_core.build_artifact(policy_name, policy.entry_fragment, fragments)
    except (compiler_core.CompileError, dsl.DSLError) as e:
        return _fail(session, service, actor, policy_name, str(e), type(e).__name__)

    for _attempt in range(MAX_ATTEMPTS):
        try:
            # Postgres 下锁定策略行，串行化并发发布的版本分配
            if session.get_bind().dialect.name == "postgresql":
                policy = session.get(Policy, policy.id, with_for_update=True)

            existing = (session.query(Artifact)
                        .filter_by(policy_name=policy_name, hash=artifact["hash"])
                        .first())
            if existing is not None:
                events.audit(session, service, "PUBLISH_DUPLICATE", actor,
                             policy=policy_name, version=existing.version,
                             hash=artifact["hash"])
                session.commit()
                return {"status": "duplicate", "policy": policy_name,
                        "version": existing.version, "hash": artifact["hash"]}

            version = policy.latest_version + 1
            session.add(Artifact(
                policy_id=policy.id, policy_name=policy_name, version=version,
                hash=artifact["hash"], nodes=artifact["nodes"],
                entry_node=artifact["entry_node"], topo=artifact["topo"],
                dep_chain=artifact["dep_chain"],
            ))
            policy.latest_version = version
            # 重建反向依赖索引（增量编译的依据）
            session.query(PolicyDep).filter_by(policy_name=policy_name).delete()
            for fname in artifact["fragments"]:
                session.add(PolicyDep(policy_name=policy_name, fragment_name=fname))
            events.audit(session, service, "PUBLISH_SUCCEEDED", actor,
                         policy=policy_name, version=version,
                         hash=artifact["hash"], dep_chain=artifact["dep_chain"])
            session.commit()
            return {"status": "published", "policy": policy_name,
                    "version": version, "hash": artifact["hash"]}
        except (IntegrityError, OperationalError):
            # 并发发布撞唯一约束 / SQLite 写锁：回滚后重读最新版本号重试
            session.rollback()
            policy = session.query(Policy).filter_by(name=policy_name).first()

    existing = (session.query(Artifact)
                .filter_by(policy_name=policy_name, hash=artifact["hash"]).first())
    if existing is not None:
        return {"status": "duplicate", "policy": policy_name,
                "version": existing.version, "hash": artifact["hash"]}
    return _fail(session, service, actor, policy_name, "concurrent_modification")


def compile_affected(session, fragment_name: str, actor: str = "anonymous",
                     service: str = "compiler") -> dict:
    """增量编译：只重编译依赖闭包中包含该片段的策略（含直接以它为入口的策略）。"""
    names = {r[0] for r in session.query(PolicyDep.policy_name)
             .filter_by(fragment_name=fragment_name).all()}
    names |= {p.name for p in session.query(Policy)
              .filter_by(entry_fragment=fragment_name).all()}
    results = [compile_policy(session, name, actor, service) for name in sorted(names)]
    return {"fragment": fragment_name, "recompiled": results}
