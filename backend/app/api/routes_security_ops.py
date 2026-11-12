# -*- coding: utf-8 -*-
"""V2.0 安全运营接口（-3；routes_security_ops.py）。

- POST /api/v1/security/llm-critic   Critic 规则+轻量语义模型 → 风险分+可追溯原因（拦截即录）
- POST /api/v1/security/ner-mask     DLP NER 识别 + 按角色/场景脱敏策略（纯脱敏，不落库）
- GET  /api/v1/security/events       安全事件工作台（处置状态/类型/严重级/案件 过滤 + 状态机视图）
- PUT  /api/v1/security/events/{id}/dispose  事件处置流转（OPEN→TRIAGE→CONFIRMED/FALSE_POSITIVE→CLOSED）

红线：只记录与展示，绝不修改判定/退款；处置流转仅更新事件自身处置状态 + 审计。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import ROLE_SUPERVISOR, get_current_user, require_min_role
from app.domain.models import User
from app.infrastructure.db import get_db
from app.services import llm_critic, ner_dlp
from app.services import security_events as se

router = APIRouter(prefix="/security", tags=["security"])


class LlmCriticRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    case_id: str | None = Field(default=None, max_length=36)
    role: str = Field(default="agent", pattern="^(customer|agent|officer)$")
    scenario: str = Field(default="refund", pattern="^(refund|voice|paper|sandbox)$")
    source: str = Field(default="critic", max_length=24)


@router.post("/llm-critic")
def llm_critic_check(
    payload: LlmCriticRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Critic 分析：规则+语义 → 风险分+原因；BLOCK/REVIEW 拦截即录 security events。

    只记录证据与事件；不做任何业务判定修改。
    """
    res = llm_critic.analyze(
        db, text=payload.text, source=payload.source,
        case_id=payload.case_id, role=payload.role, scenario=payload.scenario,
        persist=True,
    )
    body = res.as_dict()
    body["decision_id"] = res.decision_id
    body["event_ids"] = res.event_ids
    return body


class NerMaskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    role: str = Field(default="agent", pattern="^(customer|agent|officer)$")
    scenario: str = Field(default="refund", pattern="^(refund|voice|paper|sandbox)$")


@router.post("/ner-mask")
def ner_mask(
    payload: NerMaskRequest,
    user: User = Depends(get_current_user),
):
    """DLP NER 识别 + role/scenario 策略脱敏（纯函数：掩码 + 实体清单，不落库）。"""
    masked, entities = ner_dlp.mask(
        payload.text, role=payload.role, scenario=payload.scenario)
    matrix = {t: ner_dlp.resolve_action(t, payload.role, payload.scenario)
              for t in ner_dlp.ALL_ENTITY_TYPES}
    return {
        "masked_text": masked,
        "entities": [e.as_dict() for e in entities],
        "policy": {"role": payload.role, "scenario": payload.scenario,
                   "matrix": matrix},
        "red_line": "非 NONE 实体明文绝不残留",
    }


@router.get("/events")
def events_list(
    disposition: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    case_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """安全事件工作台列表（只读；newest first）。"""
    items = se.list_events(
        db, disposition=disposition, event_type=event_type,
        severity=severity, case_id=case_id, limit=limit, offset=offset,
    )
    return {"items": items, "total": len(items), "flow": se.disposition_flow()}


@router.get("/events/flow")
def events_flow(user: User = Depends(get_current_user)):
    """处置状态机视图（OPEN→TRIAGE→CONFIRMED/FALSE_POSITIVE→CLOSED）。"""
    return se.disposition_flow()


class DisposalRequest(BaseModel):
    action: str = Field(pattern="^(triage|confirm|false_positive|close)$")
    note: str | None = Field(default=None, max_length=300)


@router.put("/events/{event_id}/dispose")
def events_dispose(
    event_id: str,
    payload: DisposalRequest,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """事件处置流转（主管+）：非法跳转/非法动作 → 400；事件不存在 → 404。"""
    try:
        item = se.dispose_event(
            db, event_id=event_id, action=payload.action,
            disposed_by=str(user.id), note=payload.note,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="事件不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {"item": item}