# -*- coding: utf-8 -*-
"""退款状态独立事件流（回执/重试/审计）。

把退款状态从「RefundCase.refund_status 单字段」升级为「不可变事件流」：
每一次状态迁移（NONE/PENDING/SUCCESS/FAILED）落一条 `refund_events`，
供外部/主管/审计独立查询、回执与重试。事件带 **业务幂等 GUID**——同一 GUID
绝不重复落审计（重放不产生重复退款）。

设计要点
--------
- **guid = 幂等键**：`record_refund_event` 幂等插入（unique(guid) 冲突则跳过），
  供 Worker 重投/死信重放复用同一 GUID，审计上只有一条记录。
- **previous_status / new_status**：显式记录每次迁移前后，审计可还原状态机路径。
- **gateway_before**：退款执行前 tool_gate 判定快照（PASS/BLOCK），审计“为什么退”。
- **event_type**：QUERY / REPLAY / EXEC / OK / FAIL / RETRY——区分正常与重放。
"""
from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.models import (
    REFUND_STATUS_FAILED,
    REFUND_STATUS_NONE,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    RefundEvent,
    new_uuid,
)

EVENT_QUERY = "QUERY"
EVENT_REPLAY = "REPLAY"       # 死信/幂等重放
EVENT_EXEC = "EXEC"           # 实际执行退款
EVENT_OK = "OK"
EVENT_FAIL = "FAIL"
EVENT_RETRY = "RETRY"
EVENT_CONFIRM = "CONFIRM"     # 主管确认/驳回

REFUND_TRACKABLE_STATUSES = frozenset({
    REFUND_STATUS_NONE, REFUND_STATUS_PENDING, REFUND_STATUS_SUCCESS, REFUND_STATUS_FAILED,
})


def _guid() -> str:
    return f"re-{uuid.uuid4().hex}"


def record_refund_event(
    db: Session,
    *,
    case_id: str,
    event_type: str,
    previous_status: str,
    new_status: str,
    guid: str | None = None,
    gateway_before: str | None = None,
    reason: str | None = None,
    set_case_status: bool = True,
) -> RefundEvent:
    """落一条退款事件（幂等，unique guid 冲突跳过）。

    - 若 `guid` 已存在（重放/重投）跳过插入并返回既有行；
    - `set_case_status=True` 时同步更新 `RefundCase.refund_status`（管理员确认后
      由调用方显式传 False 避免覆盖终态）。
    """
    guid = guid or _guid()
    existing = db.query(RefundEvent).filter_by(guid=guid).first()
    if existing is not None:
        if new_status != existing.new_status:
            # 重放 GUID 但状态不一致是异常：保留首条（审计不可覆盖），记录 retry 线索
            record_refund_event(
                db, case_id=case_id,
                event_type=EVENT_RETRY,
                previous_status=existing.new_status,
                new_status=new_status,
                reason=f"replay guid {guid} 状态不一致，审计保留首条",
                gateway_before=gateway_before,
                set_case_status=False,
            )
        return existing

    evt = RefundEvent(
        id=new_uuid(),
        case_id=case_id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        guid=guid,
        gateway_before=gateway_before,
        reason=reason,
    )
    db.add(evt)
    db.flush()
    if set_case_status:
        case = db.get(RefundCase, case_id)
        if case is not None:
            case.refund_status = new_status
            if case.refund_events_guid is None:
                case.refund_events_guid = guid   # 挂链：案件指向最近事件
    db.commit()
    return evt


def query_refund_status(db: Session, case_id: str) -> dict:
    """独立退款状态查询（含事件轨迹）。"""
    case = db.get(RefundCase, case_id)
    if case is None:
        raise KeyError(f"case {case_id} not found")
    events = (
        db.query(RefundEvent)
        .filter_by(case_id=case_id)
        .order_by(RefundEvent.created_at, RefundEvent.id)
        .all()
    )
    return {
        "case_id": case_id,
        "refund_status": case.refund_status,
        "last_guid": case.refund_events_guid,
        "events": [
            {
                "guid": e.guid,
                "event_type": e.event_type,
                "previous_status": e.previous_status,
                "new_status": e.new_status,
                "gateway_before": e.gateway_before,
                "reason": e.reason,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


def retry_refund(db: Session, case_id: str, *, guid: str | None = None,
                 reason: str = "manual retry") -> dict:
    """人工/审计触发退款重试（幂等，不重复退款）。"""
    case = db.get(RefundCase, case_id)
    if case is None:
        raise KeyError(f"case {case_id} not found")
    evt = record_refund_event(
        db, case_id=case_id,
        event_type=EVENT_REPLAY,
        previous_status=case.refund_status if case.refund_status else REFUND_STATUS_NONE,
        new_status=case.refund_status if case.refund_status else REFUND_STATUS_NONE,
        guid=guid,
        reason=reason,
        set_case_status=False,      # 重试不改状态（状态机不回滚）
    )
    return {"case_id": case_id, "replayed": True, "guid": evt.guid,
            "refund_status": case.refund_status}


def confirm_refund_event(db: Session, case_id: str, *, new_status: str,
                         opinion: str, confirmed_by: str | None = None,
                         guid: str | None = None) -> RefundEvent:
    """主管确认/驳回退款（reason-confirm 端点）。校验状态在合法集合。"""
    if new_status not in REFUND_TRACKABLE_STATUSES:
        raise ValueError(f"非法退款状态: {new_status}")
    case = db.get(RefundCase, case_id)
    if case is None:
        raise KeyError(f"case {case_id} not found")
    prev = case.refund_status or REFUND_STATUS_NONE
    evt = record_refund_event(
        db, case_id=case_id,
        event_type=EVENT_CONFIRM,
        previous_status=prev,
        new_status=new_status,
        guid=guid,
        reason=opinion,
        set_case_status=True,
    )
    return evt