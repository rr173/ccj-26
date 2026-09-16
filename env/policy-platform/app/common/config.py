import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./policy.db")
SERVICE_NAME = os.getenv("SERVICE_NAME", "service")
COMPILER_URL = os.getenv("COMPILER_URL", "http://localhost:8002")

# 单次查询默认/最大执行预算（毫秒）
DEFAULT_TIMEOUT_MS = int(os.getenv("QUERY_TIMEOUT_MS", "200"))
MAX_TIMEOUT_MS = int(os.getenv("QUERY_MAX_TIMEOUT_MS", "5000"))

# 调试会话租约默认时长（秒）：到期后租约可被他人接管
DEBUG_DEFAULT_LEASE_S = int(os.getenv("DEBUG_LEASE_TTL_S", "60"))
DEBUG_MAX_LEASE_S = int(os.getenv("DEBUG_LEASE_MAX_S", "3600"))

# 限额台账（quota :8005）
# 判定占用的默认/最大超时（秒）：异常退出或超时后占用自动归还到产生周期
QUOTA_DEFAULT_TTL_S = int(os.getenv("QUOTA_TTL_S", "60"))
QUOTA_MAX_TTL_S = int(os.getenv("QUOTA_TTL_MAX_S", "86400"))
# 三个可分开启动的组件：collector（凭证采集）/ gate（前置余额门禁+占用超时回收）/
# accountant（批次核算、封账、补冲账）。"all" 表示同进程全部启动。
QUOTA_ROLE = os.getenv("QUOTA_ROLE", "all")
QUOTA_WORKER_INTERVAL_MS = int(os.getenv("QUOTA_WORKER_INTERVAL_MS", "250"))
