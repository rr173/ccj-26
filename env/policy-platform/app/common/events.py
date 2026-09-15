"""审计事件写入。与业务写入同事务提交，保证状态变更必留痕。"""
from .models import AuditEvent


def audit(session, service: str, event_type: str, actor=None, **payload) -> None:
    session.add(AuditEvent(
        service=service,
        event_type=event_type,
        actor=actor or "anonymous",
        payload=payload,
    ))
