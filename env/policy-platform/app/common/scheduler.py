"""提案生效调度器：后台线程周期性把到点的 SCHEDULED 提案按确定顺序生效。

状态全部在数据库，线程只做"到期触发"：
- 服务重启后线程重建：复位崩溃残留的 APPLYING，立即跑一个周期（宕机期间到点的立即生效），
  未到点的预约继续等到点；等待评审的提案不受影响（意见在库里）。
"""
import os
import threading
import time

from . import events, proposal_service as ps
from .db import SessionLocal

_stop = threading.Event()
_thread = None


def _interval_ms() -> int:
    try:
        return max(20, int(os.getenv("SCHEDULER_INTERVAL_MS", "500")))
    except ValueError:
        return 500


def _cycle(service: str):
    try:
        results = ps.run_due(SessionLocal, actor="scheduler")
        applied = [r for r in results if r.get("status") == ps.EFFECTIVE]
        if applied:
            with SessionLocal() as s:
                events.audit(s, service, "SCHEDULER_APPLIED", "scheduler",
                             results=results)
                s.commit()
    except Exception as e:  # noqa: BLE001 - 调度循环不能因单周期异常退出
        with SessionLocal() as s:
            events.audit(s, service, "SCHEDULER_ERROR", "scheduler", error=str(e))
            s.commit()


def _run(service: str):
    while not _stop.wait(_interval_ms() / 1000.0):
        _cycle(service)


def start(service: str = "editor", *, run_immediately: bool = True):
    """幂等启动（每个进程一个后台线程）。"""
    global _thread
    with SessionLocal() as s:
        recovered = ps.recover_applying(s)
        if recovered:
            events.audit(s, service, "SCHEDULER_RECOVERED", "scheduler",
                         recovered=recovered)
            s.commit()
    if run_immediately:
        _cycle(service)
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_run, args=(service,), daemon=True,
                                   name="proposal-scheduler")
        _thread.start()


def stop(timeout: float = 2.0):
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout)
        _thread = None


def trigger_cycle(service: str = "editor"):
    """手动触发一个调度周期（测试/演示用）。"""
    _cycle(service)
