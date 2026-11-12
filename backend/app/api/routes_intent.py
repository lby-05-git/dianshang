# -*- coding: utf-8 -*-
"""意图路由端点（基线 V1.0）。

POST /api/v1/intent/route —— 两级意图：(规则层命中即定) -> (LLM 层) -> Fail-Safe 转 HUMAN。
纯服务层调用 intent_router，落 intent_results；不改变业务状态机。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.domain.models import User
from app.infrastructure.db import get_db
from app.services.intent_router import route_intent

router = APIRouter(prefix="/intent", tags=["intent"])


@router.post("/route")
def intent_route(
    text: str,
    case_id: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """两级意图路由（规则 -> LLM -> Fail-Safe 转 HUMAN）。"""
    res = route_intent(db, case_id=case_id or "standalone", text=text,
                       llm_route=None, persist=False)   # 纯服务判定不落库(standalone 无案件)
    return res.as_dict()