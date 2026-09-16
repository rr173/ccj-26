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
