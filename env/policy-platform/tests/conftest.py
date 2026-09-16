import os
import tempfile

# 必须在导入任何 app 模块之前设置（engine 在 import 时创建）
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ["SERVICE_NAME"] = "test"
# 测试中关闭后台调度器的周期触发（调度周期拉长），生效由显式调用/重启生命周期验证
os.environ.setdefault("SCHEDULER_INTERVAL_MS", "600000")

import pytest  # noqa: E402

from app.common import models  # noqa: E402
from app.common.db import SessionLocal, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    with SessionLocal() as s:
        for t in (models.ProposalEvent, models.Review, models.Proposal,
                  models.ApprovalConfig,
                  models.Decision, models.AuditEvent, models.PolicyDep,
                  models.Artifact, models.Policy, models.Fragment):
            s.query(t).delete()
        s.commit()
    import app.runtime.main as rt
    rt.cache.clear()
    yield
