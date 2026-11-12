# -*- coding: utf-8 -*-
"""周期意图对照评测 API。

读取和一键执行向 supervisor 开放；调度的创建/启停继续由
evaluation-center/schedules 的 leader 权限治理。所有任务只读回放，
不修改订单、案件或退款状态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import ROLE_SUPERVISOR, require_min_role
from app.domain.models import User
from app.infrastructure.audit import write_event
from app.infrastructure.db import get_db
from app.services import intent_periodic_eval as ipe


router = APIRouter(
    prefix="/evaluation-center/intent-evaluation",
    tags=["evaluation-center"],
)


@router.get("/latest")
def latest_intent_evaluation(
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    item = ipe.latest_intent_run(db)
    active = ipe.active_intent_run(db)
    return {
        "status": ("RUNNING" if active is not None
                   else "READY" if item and item.get("status") == "COMPLETED"
                   else "PENDING"),
        "item": item,
        "active_run_id": active.run_id if active is not None else None,
        "dataset": {
            "version": ipe.DATASET_VERSION,
            "sample_count": len(ipe.build_intent_test_set()),
            "checksum": ipe.dataset_checksum(),
            "shopping_flow_sample_count": 100,
            "total_case_count": len(ipe.build_intent_test_set()) + 100,
        },
        "note": "意图 TTFT/Token 估算与真实 usage 分开标记；商城链路使用真实 OCR，且不写业务订单、不执行退款。",
    }


@router.get("/runs")
def intent_evaluation_runs(
    limit: int = 20,
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    return ipe.list_intent_runs(db, limit=limit)


@router.post("/runs", status_code=202)
def create_intent_evaluation(
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """一键执行意图对照评测；对活动任务幂等去重。"""
    existing = ipe.active_intent_run(db)
    if existing is not None:
        # PENDING 投递窗口中断时重投同一 run；Worker 状态门+锁去重。
        if existing.status == "PENDING":
            ipe.enqueue_intent_run(existing)
        return {
            "run_id": existing.run_id,
            "status": existing.status,
            "deduplicated": True,
            "message": "已有意图评测正在执行",
        }

    run = ipe.create_intent_eval_run(
        db, created_by=str(user.id), trigger_source="manual",
    )
    ipe.enqueue_intent_run(run)
    write_event(
        db, actor_id=str(user.id), case_id=None,
        action="INTENT_EVAL_STARTED",
        payload={
            "run_id": run.run_id,
            "dataset_version": ipe.DATASET_VERSION,
            "intent_sample_count": len(ipe.build_intent_test_set()),
            "shopping_flow_sample_count": 100,
            "total_case_count": len(ipe.build_intent_test_set()) + 100,
            "read_only": True,
        },
    )
    db.commit()
    return {
        "run_id": run.run_id,
        "status": run.status,
        "deduplicated": False,
        "message": "120 条意图对照 + 100 条商城退换货 OCR 链路评测已入队",
    }
