# -*- coding: utf-8 -*-
"""安全事件工作台（V2.0 -3；）。

职责：
- 记录：DLP/注入/越权/工具门禁 命中即录一条 SecurityEvent（只记录，不改判定）；
- 处置流转：OPEN → TRIAGE → CONFIRMED / FALSE_POSITIVE → CLOSED；
  - 每条处置变更写一条 DomainEvent 审计（append-only）；
  - 非法跳转（如 OPEN→CLOSED / CONFIRMED→TRIAGE）拒绝并抛出 ValueError。
- 查询：列表（按处置状态 / 类型 / 严重级 / 案件过滤）、详情（只读）。

红线：
- 本模块绝不修改 security_decisions / refund_cases / 任何判定结果；
- detail 只存掩码片段（DLP 红线，明文绝不入库）；audit_sha256 存原文哈希用于取证。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models import SecurityEvent, new_uuid
from app.infrastructure.audit import write_event

# ---- 事件类型 ----
SEC_EV_INJECTION = "INJECTION"          # 注入/越狱/伪造授权 拦截
SEC_EV_DLP = "DLP"                      # 敏感信息检测（PII 脱敏触发）
SEC_EV_TOOL_GATE = "TOOL_GATE"          # 工具门禁（退款/审批/文档/通知/外部查询）拦截

EVENT_TYPES = (SEC_EV_INJECTION, SEC_EV_DLP, SEC_EV_TOOL_GATE)

# ---- 处置状态机 ----
DISP_OPEN = "OPEN"
DISP_TRIAGE = "TRIAGE"
DISP_CONFIRMED = "CONFIRMED"
DISP_FALSE_POSITIVE = "FALSE_POSITIVE"
DISP_CLOSED = "CLOSED"

# 合法流转：OPEN→TRIAGE；TRIAGE→CONFIRMED/FALSE_POSITIVE；CONFIRMED|FALSE_POSITIVE→CLOSED
DISP_GRAPH: dict[str, tuple[str, ...]] = {
    DISP_OPEN: (DISP_TRIAGE,),
    DISP_TRIAGE: (DISP_CONFIRMED, DISP_FALSE_POSITIVE),
    DISP_CONFIRMED: (DISP_CLOSED,),
    DISP_FALSE_POSITIVE: (DISP_CLOSED,),
    DISP_CLOSED: (),
}

# 处置动作：action → 目标状态（前端/工作台按钮）
DISP_ACTIONS: dict[str, str] = {
    "triage": DISP_TRIAGE,               # 进入研判
    "confirm": DISP_CONFIRMED,           # 确认为真实安全事件
    "false_positive": DISP_FALSE_POSITIVE,  # 判定为误报
    "close": DISP_CLOSED,                # 结案
}

# 严重级（复用 security_decisions 的 LOW/MEDIUM/HIGH）
SEV_LOW = "low"
SEV_MEDIUM = "medium"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"
SEVERITIES = (SEV_LOW, SEV_MEDIUM, SEV_HIGH, SEV_CRITICAL)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_event(
    db: Session,
    *,
    event_type: str,
    detail: str | None = None,
    case_id: str | None = None,
    severity: str = SEV_MEDIUM,
    audit_sha256: str | None = None,
) -> SecurityEvent:
    """记录一条安全事件（只 insert）。detail 必须已是掩码片段（调用方承诺）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知事件类型: {event_type}")
    if severity not in SEVERITIES:
        raise ValueError(f"非法严重级: {severity}")
    evt = SecurityEvent(
        id=new_uuid(),
        case_id=case_id,
        event_type=event_type,
        severity=severity,
        detail=(detail or "")[:2000],
        disposition=DISP_OPEN,
        audit_sha256=audit_sha256,
    )
    db.add(evt)
    db.flush()
    return evt


def list_events(
    db: Session,
    *,
    disposition: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    case_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """安全事件列表（只读，newest first）。"""
    q = db.query(SecurityEvent)
    if disposition and disposition != "ALL":
        q = q.filter(SecurityEvent.disposition == disposition)
    if event_type:
        q = q.filter(SecurityEvent.event_type == event_type)
    if severity:
        q = q.filter(SecurityEvent.severity == severity)
    if case_id:
        q = q.filter(SecurityEvent.case_id == case_id)
    rows = (
        q.order_by(SecurityEvent.created_at.desc(), SecurityEvent.id.desc())
        .offset(offset).limit(limit).all()
    )
    return [_as_dict(r) for r in rows]


def get_event(db: Session, event_id: str) -> dict | None:
    row = db.get(SecurityEvent, event_id)
    return None if row is None else _as_dict(row)


def _as_dict(row: SecurityEvent) -> dict:
    return {
        "id": row.id,
        "case_id": row.case_id,
        "event_type": row.event_type,
        "severity": row.severity,
        "detail": row.detail,
        "disposition": row.disposition,
        "disposed_by": row.disposed_by,
        "disposed_at": row.disposed_at.isoformat() if row.disposed_at else None,
        "audit_sha256": row.audit_sha256,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def disposition_flow() -> dict:
    """状态机对外视图（开放 API 展示 / 前端渲染）。"""
    return {
        "start": DISP_OPEN,
        "states": list(DISP_GRAPH),
        "graph": {k: list(v) for k, v in DISP_GRAPH.items()},
        "actions": DISP_ACTIONS,
    }


def dispose_event(
    db: Session,
    *,
    event_id: str,
    action: str,
    disposed_by: str,
    note: str | None = None,
) -> dict:
    """处置流转：事件按 state-machine 前进；每次变更落 DomainEvent 审计。

    非法动作 / 非法跳转 → ValueError（路由层映射 400）。
    只更新 safety domain（disposition / disposed_by / disposed_at）与审计，不改判定。
    """
    row = db.get(SecurityEvent, event_id)
    if row is None:
        raise KeyError(event_id)
    target = DISP_ACTIONS.get(action)
    if target is None:
        raise ValueError(f"非法处置动作: {action}（可选 {sorted(DISP_ACTIONS)}）")
    if target not in DISP_GRAPH.get(row.disposition, ()):
        raise ValueError(
            f"非法跳转: {row.disposition} → {target} 不被允许"
            f"（合法: {DISP_GRAPH.get(row.disposition, ())}）"
        )
    row.disposition = target
    row.disposed_by = disposed_by
    row.disposed_at = _now()
    db.flush()
    write_event(
        db,
        actor_id=disposed_by,
        case_id=row.case_id,
        action="SECURITY_EVENT_DISPOSED",
        payload={"event_id": event_id, "to": target, "note": note or ""},
    )
    return _as_dict(row)