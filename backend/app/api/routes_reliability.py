# -*- coding: utf-8 -*-
"""意图可靠性端点（V2.0 -4 / 计划 新增 routes_reliability.py）。

覆盖：多意图拆分/冲突检测、线上误路由反馈采样（PENDING->REVIEWED->ADOPTED/REJECTED）、
死信任务查询/修复/人工重放（不重复退款）。

路径契约（计划 接口表）：
  POST /api/v1/intent/multi                 多意图拆分/冲突检测
  POST /api/v1/intent/feedback-sample       线上误路由/人工纠正入候选集
  GET  /api/v1/intent/feedback              反馈采样工作台列表
  PUT  /api/v1/intent/feedback/{id}/review  审核流转
  GET  /api/v1/intent/feedback/export       ADOPTED 候选导出（golden 重建用）
  GET  /api/v1/dead-letter                  死信任务查询
  GET  /api/v1/dead-letter/{id}             单条死信详情
  PUT  /api/v1/dead-letter/{id}/repair      修复（暂无处理逻辑的非 refund 可补 handler）
  POST /api/v1/dead-letter/{id}/replay      人工重放（幂等，不重复退款）

权限：反馈与死信均属安全/运营操作，主管及以上（数据不落地，仅状态流转）。
"""
from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import ROLE_SUPERVISOR, get_current_user, require_min_role
from app.domain.models import User
from app.infrastructure.db import get_db
from app.services import dead_letter as dl
from app.services import observability as obs
from app.services.intent_router import (
    analyze_multi_intents,
    export_feedback_candidates,
    list_feedback_samples,
    record_feedback_sample,
    review_feedback_sample,
)

intent_router = APIRouter(prefix="/intent", tags=["reliability-intent"])
dl_router = APIRouter(prefix="/dead-letter", tags=["reliability-dead-letter"])


# ---------- 请求体 ----------

class MultiRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class FeedbackRequest(BaseModel):
    case_id: str | None = None
    route_final_intent: str
    human_corrected_intent: str
    reason: str | None = Field(default=None, max_length=200)


class ReviewRequest(BaseModel):
    decision: str                     # REVIEWED / ADOPTED / REJECTED
    reason: str | None = Field(default=None, max_length=200)


class RepairRequest(BaseModel):
    task_type: str | None = None
    payload: dict | None = None
    error: str | None = None
    reopen: bool = False


# ---------- 意图多拆分 / 冲突检测 ----------

@intent_router.post("/multi")
def intent_multi_analysis(
    body: MultiRequest,
    user: User = Depends(get_current_user),
):
    """多意图拆分 + 冲突检测（只读分析，不落库，可安全重复调用）。"""
    analysis = analyze_multi_intents(body.text)
    return analysis.as_dict()


@intent_router.get("/metrics")
def intent_metrics(
    days: int = 7,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """意图可靠性看板：规则命中率/LLM调用率/召回率/幻觉率/Token（只读聚合）。"""
    return obs.intent_metrics(db, days=days)


# ---------- 反馈采样（线上误路由/人工纠正 -> 候选集） ----------

@intent_router.post("/feedback-sample")
def feedback_sample(
    body: FeedbackRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """落一条线上反馈采样（PENDING）。客服/主管手动纠正即入库。"""
    try:
        row = record_feedback_sample(
            db, case_id=body.case_id,
            route_final_intent=body.route_final_intent,
            human_corrected_intent=body.human_corrected_intent,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": row.id, "status": row.status}


@intent_router.get("/feedback")
def feedback_list(
    status: str | None = None,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    return list_feedback_samples(db, status=status)


@intent_router.put("/feedback/{sample_id}/review")
def feedback_review(
    sample_id: str,
    body: ReviewRequest,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """审核：PENDING->REVIEWED->(ADOPTED|REJECTED)。非法跳级 400。"""
    try:
        return review_feedback_sample(
            db, sample_id, decision=body.decision,
            reviewed_by=str(user.id), reason=body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@intent_router.get("/feedback/export")
def feedback_export(
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """导出 ADOPTED 候选（黄金集重建时消费，绝不自动改冻结套件）。"""
    return {"candidates": export_feedback_candidates(db)}


# ---------- 死信任务（查询/修复/人工重放） ----------

@dl_router.get("")
def dead_letter_list(
    status: str | None = None,
    task_type: str | None = None,
    limit: int = 50,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    return {"items": dl.list_dead_letters(db, status=status, task_type=task_type,
                                          limit=min(limit, 500))}


@dl_router.get("/{dl_id}")
def dead_letter_get(
    dl_id: str,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    try:
        return dl.get_dead_letter(db, dl_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@dl_router.put("/{dl_id}/repair")
def dead_letter_repair(
    dl_id: str,
    body: RepairRequest,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """修复：改 task_type/payload/error，或 FAILED 重新 OPEN。"""
    try:
        return dl.repair_dead_letter(
            db, dl_id, task_type=body.task_type, payload=body.payload,
            error=body.error, reopen=body.reopen)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@dl_router.post("/{dl_id}/replay")
def dead_letter_replay(
    dl_id: str,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """人工重放：退款任务走内置幂等 retry_refund（guid 唯一，不重复退款）；
    其它任务类型缺 handler -> ValueError + 置回 FAILED（诚实不留假成功）。"""
    try:
        return dl.replay_dead_letter(db, dl_id, replayed_by=str(user.id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))