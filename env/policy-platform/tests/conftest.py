import os
import tempfile

# 必须在导入任何 app 模块之前设置（engine 在 import 时创建）
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["SERVICE_NAME"] = "test"
# 测试中关闭后台调度器的周期触发（调度周期拉长），生效由显式调用/重启生命周期验证
os.environ.setdefault("SCHEDULER_INTERVAL_MS", "600000")
# quota 后台工作器周期同样拉长：测试用 /quota/tick/{role} 手动驱动
os.environ.setdefault("QUOTA_WORKER_INTERVAL_MS", "600000")

import pytest  # noqa: E402

from app.common import models  # noqa: E402
from app.common.db import SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    # 只追加表有数据库级 UPDATE/DELETE 触发器，测试间重置用 drop+重建而非 DELETE
    immutable_tables = (models.QuotaAdjustmentEntry, models.QuotaAdjustment,
                        models.QuotaPageLine, models.QuotaPage,
                        models.QuotaRelease, models.QuotaSettlement)
    for t in immutable_tables:
        t.__table__.drop(bind=engine, checkfirst=True)
    with SessionLocal() as s:
        for t in (models.QuotaVoucher, models.QuotaHold, models.QuotaInboxEvent,
                  models.QuotaBatch, models.QuotaVersion, models.QuotaRule,
                  models.QuotaAccount, models.QuotaMeta,
                  models.DebugEpoch, models.DebugCommand, models.DebugEvent,
                  models.DebugFrame, models.DebugBranch, models.DebugSession,
                  models.ProposalEvent, models.Review, models.Proposal,
                  models.ApprovalConfig,
                  models.Decision, models.AuditEvent, models.PolicyDep,
                  models.Artifact, models.Policy, models.Fragment):
            s.query(t).delete()
        s.commit()
    from app.common.db import (Base, install_pg_append_only_triggers,
                               install_sqlite_append_only_triggers)
    Base.metadata.create_all(
        bind=engine, checkfirst=True,
        tables=[t.__table__ for t in reversed(immutable_tables)])
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            install_sqlite_append_only_triggers(conn)
        else:
            install_pg_append_only_triggers(conn)
    import app.runtime.main as rt
    rt.cache.clear()
    yield
