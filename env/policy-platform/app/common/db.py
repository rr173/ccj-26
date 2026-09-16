import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# SQLite 下调试器后台线程 + API 线程会并发写，放宽内置锁等待避免 "database is locked"
_is_sqlite = DATABASE_URL.startswith("sqlite")
_connect_args = ({"check_same_thread": False, "timeout": 30}
                 if _is_sqlite else {})
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()

# SQLite 事务开始模式（线程局部，默认 DEFERRED，保持 SQLAlchemy 原行为）：
# 限额写临界区进入前用 immediate() 置位，本线程下一个事务以 BEGIN IMMEDIATE
# 开启，「读余额 -> 判定 -> 写占用」整段持 RESERVED 写锁串行，杜绝并发超卖；
# 其它服务（调试器后台长事务等）仍走 DEFERRED，读不阻塞写、写不阻塞读。
_tx_state = threading.local()


@contextmanager
def immediate():
    prev = getattr(_tx_state, "immediate", False)
    _tx_state.immediate = True
    try:
        yield
    finally:
        _tx_state.immediate = prev


# ---------------------------------------------------------------------------
# 台账/限额相关表为「只追加」表：销账、归还、账页、账页行、调整分录一旦写入，
# 任何 UPDATE/DELETE 都必须被数据库拒绝（账页不得被覆盖是硬约束，不仅是应用约定）。
# ---------------------------------------------------------------------------
_IMMUTABLE_TABLES = (
    "quota_settlements",
    "quota_releases",
    "quota_pages",
    "quota_page_lines",
    "quota_adjustment_entries",
)


def install_sqlite_append_only_triggers(conn):
    for t in _IMMUTABLE_TABLES:
        conn.execute(text(
            f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_update "
            f"BEFORE UPDATE ON {t} BEGIN "
            f"SELECT raise(ABORT, '{t} is append-only'); END;"))
        conn.execute(text(
            f"CREATE TRIGGER IF NOT EXISTS trg_{t}_no_delete "
            f"BEFORE DELETE ON {t} BEGIN "
            f"SELECT raise(ABORT, '{t} is append-only'); END;"))


def install_pg_append_only_triggers(conn):
    conn.execute(text(
        "CREATE OR REPLACE FUNCTION quota_append_only_guard() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'table % is append-only; % forbidden', "
        "TG_TABLE_NAME, TG_OP; END; $$ LANGUAGE plpgsql;"))
    for t in _IMMUTABLE_TABLES:
        for op in ("UPDATE", "DELETE"):
            conn.execute(text(
                f"DROP TRIGGER IF EXISTS trg_{t.lower()}_no_{op.lower()} ON {t};"))
            conn.execute(text(
                f"CREATE TRIGGER trg_{t.lower()}_no_{op.lower()} "
                f"BEFORE {op} ON {t} "
                f"FOR EACH ROW EXECUTE FUNCTION quota_append_only_guard();"))


# Python 3.11 的 sqlite3 连接不允许覆写 begin()，用 isolation_level=None 接管事务，
# 在 before_cursor_execute 里按下述规则显式发 BEGIN（复刻并增强 pysqlite 原语义）：
#   - SELECT 不开启事务：autocommit 读最新已提交数据（调试器「heartbeat 写锁 ->
#     再读租约」等依赖读后不持有旧快照的既有行为不变）；
#   - 任意 DML 自动 BEGIN IMMEDIATE：写者从第一条写语句起持 RESERVED 锁排队；
#   - quota 写临界区（线程标志）第一条语句（含 SELECT）即 BEGIN IMMEDIATE，
#     保证「读余额 -> 判定 -> 写占用」全程串行，杜绝并发超卖。
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
        dbapi_conn.isolation_level = None  # 关闭 pysqlite 隐式事务，由下面显式发 BEGIN

    def _stmt_is_dml(statement: str) -> bool:
        head = statement.lstrip().lstrip("(").lstrip().upper()
        return head.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE",
                                "INSERT OR REPLACE"))

    @event.listens_for(engine, "before_cursor_execute")
    def _sqlite_before_execute(conn, cursor, statement, parameters, context,
                               executemany):
        dbapi = cursor.connection
        if getattr(dbapi, "in_transaction", False):
            return  # 事务已开启（IMMEDIATE），沿用之
        if getattr(_tx_state, "immediate", False) or _stmt_is_dml(statement):
            cursor.execute("BEGIN IMMEDIATE")


def init_db() -> None:
    from . import models  # noqa: F401  (注册表结构)

    Base.metadata.create_all(engine)
    # 只追加表的数据库级护栏（与表创建同一次部署完成）
    with engine.begin() as conn:
        if _is_sqlite:
            install_sqlite_append_only_triggers(conn)
        else:
            install_pg_append_only_triggers(conn)
