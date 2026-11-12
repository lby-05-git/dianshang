# -*- coding: utf-8 -*-
"""漂移监测 + 成本预算告警 API（M20 V3.0 ）。

`/api/v1/evaluation-center/monitor`：
  - GET/POST /budgets              预算设置（dimension×dim_key×period 幂等）
  - GET      /drift                漂移指标历史
  - GET      /drift/summary/{suite_id}  某套件最新一轮指标 + 是否漂移
  - GET/POST /alerts               告警列表 / 触发一次预算检查
  - POST     /alerts/{alert_id}/ack 人工确认告警

红线：全部只读/只写观测数据，绝不触碰退款/评测决策。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import ROLE_LEADER, require_min_role
from app.domain.models import User
from app.infrastructure.db import get_db
from app.services import observability as obs

router = APIRouter(prefix="/evaluation-center/monitor", tags=["evaluation-center"])


class BudgetBody(BaseModel):
    dimension: str = Field(pattern="^(env|model|prompt|agent|task_type|run)$")
    dim_key: str = Field(min_length=1, max_length=80)
    period: str = Field(pattern="^(daily|weekly|monthly)$")
    limit_amount: float = Field(ge=0, default=100.0)
    limit_tokens: int = Field(ge=0, default=0)
    enabled: bool = True


class CheckBudgetBody(BaseModel):
    dimension: str = Field(pattern="^(env|model|prompt|agent|task_type|run)$")
    dim_key: str = Field(min_length=1, max_length=80)
    period: str = Field(pattern="^(daily|weekly|monthly)$")
    amount: float = Field(ge=0, default=0)
    tokens: int = Field(ge=0, default=0)


@router.get("/budgets")
def budgets_list(user: User = Depends(require_min_role(ROLE_LEADER)),
                 db: Session = Depends(get_db)):
    return {"items": obs.list_budgets(db)}


@router.post("/budgets", status_code=201)
def budgets_upsert(body: BudgetBody,
                   user: User = Depends(require_min_role(ROLE_LEADER)),
                   db: Session = Depends(get_db)):
    item = obs.upsert_budget(db, dimension=body.dimension, dim_key=body.dim_key,
                             period=body.period, limit_amount=body.limit_amount,
                             limit_tokens=body.limit_tokens, enabled=body.enabled,
                             owner_id=str(user.id))
    return {"item": item}


@router.get("/drift")
def drift_list(user: User = Depends(require_min_role(ROLE_LEADER)),
               db: Session = Depends(get_db)):
    return {"items": obs.list_drift_indicators(db)}


@router.get("/drift/summary/{suite_id}")
def drift_summary(suite_id: str,
                 user: User = Depends(require_min_role(ROLE_LEADER)),
                 db: Session = Depends(get_db)):
    return {"item": obs.latest_drift_summary(db, suite_id)}


@router.get("/drift/production")
def drift_production(user: User = Depends(require_min_role(ROLE_LEADER)),
                     db: Session = Depends(get_db)):
    """生产 Trace 漂移摘要（V3.2 ）：低比例脱敏抽样的窗口指标只读摘要。

    数据由 drift_monitor.sweep() 落库（prod/* metric_key），绝不包含明文 PII。
    """
    from app.services import drift_monitor
    return {"item": drift_monitor.production_drift_summary(db)}


@router.get("/alerts")
def alerts_list(unacked: bool = False,
                user: User = Depends(require_min_role(ROLE_LEADER)),
                db: Session = Depends(get_db)):
    return {"items": obs.list_alerts(db, unacked_only=unacked)}


@router.post("/alerts/check", status_code=202)
def alerts_check(body: CheckBudgetBody,
                 user: User = Depends(require_min_role(ROLE_LEADER)),
                 db: Session = Depends(get_db)):
    """按维度触发一次预算检查（幂等：同 run/metric 已告警不重复）。"""
    alerts = obs.check_budget(db, dimension=body.dimension, dim_key=body.dim_key,
                              period=body.period, amount=body.amount,
                              tokens=body.tokens)
    return {"items": alerts}


@router.post("/alerts/{alert_id}/ack")
def alerts_ack(alert_id: str,
               user: User = Depends(require_min_role(ROLE_LEADER)),
               db: Session = Depends(get_db)):
    item = obs.ack_alert(db, alert_id, acked_by=str(user.id))
    if item is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return {"item": item}


# ---- V3-4 意图质量看板（-意图：召回/幻觉/Token 对比）----

@router.get("/intent/eval-metrics")
def intent_eval_metrics(window_days: int = 7,
                        case_prefix: str | None = None,
                        user: User = Depends(require_min_role(ROLE_LEADER)),
                        db: Session = Depends(get_db)):
    """意图质量（实测口径）：退赔召回 ≥90%、幻觉 ≤2%。未测得如实 None/False。"""
    from app.services import intent_drift as idf
    return {"item": idf.intent_eval_metrics(
        db, window_days=window_days, case_prefix=case_prefix)}


@router.get("/intent/token-comparison")
def intent_token_comparison(user: User = Depends(require_min_role(ROLE_LEADER))):
    """长期对比看板：纯 LLM vs 混合路由 Token（降低 ≥40% 实测口径）。"""
    return {"item": obs.intent_hybrid_token_report()}


@router.get("/intent/drift")
def intent_drift_items(limit: int = 5,
                       user: User = Depends(require_min_role(ROLE_LEADER)),
                       db: Session = Depends(get_db)):
    """意图质量漂移摘要（只读；漂移由 sweep_intent_drift 产生）。"""
    from app.services import intent_drift as idf
    return {"item": idf.intent_drift_summary(db, limit=limit)}