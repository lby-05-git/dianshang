# -*- coding: utf-8 -*-
"""审计基础设施：write_event 写 domain_events（append-only，3.3.7）。

- domain_events 表只 insert，不允许 UPDATE/DELETE（红线）。
- 供各路由调用：审批/认领/转派/策略变更/导出/退款回执等敏感动作落一条审计。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models import DomainEvent


def write_event(
    db: Session,
    *,
    actor_id: str | None = None,
    case_id: str | None = None,
    action: str,
    payload: dict | None = None,
) -> DomainEvent:
    """写一条领域事件（纯追加）。返回已持久化的事件对象。"""
    evt = DomainEvent(
        actor_id=actor_id,
        case_id=case_id,
        action=action,
        payload=payload or {},
    )
    db.add(evt)
    db.flush()
    return evt


def current_ts() -> str:
    return datetime.now(timezone.utc).isoformat()