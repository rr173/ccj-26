from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# SQLite 下调试器后台线程 + API 线程会并发写，放宽内置锁等待避免 "database is locked"
_connect_args = ({"check_same_thread": False, "timeout": 30}
                 if DATABASE_URL.startswith("sqlite") else {})
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def init_db() -> None:
    from . import models  # noqa: F401  (注册表结构)

    Base.metadata.create_all(engine)
