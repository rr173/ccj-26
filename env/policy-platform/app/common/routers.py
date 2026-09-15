"""共享只读路由：审计日志查询。三个服务都挂载，任何部署单元都能查审计。"""
from typing import Optional

from fastapi import APIRouter, Query

from .db import SessionLocal
from .models import AuditEvent
from .serialize import audit_dict

audit_router = APIRouter()


@audit_router.get("/audit")
def list_audit(event_type: Optional[str] = None,
               service: Optional[str] = None,
               limit: int = Query(default=100, le=1000)):
    with SessionLocal() as s:
        q = s.query(AuditEvent).order_by(AuditEvent.id.desc())
        if event_type:
            q = q.filter_by(event_type=event_type)
        if service:
            q = q.filter_by(service=service)
        return [audit_dict(e) for e in q.limit(limit).all()]
