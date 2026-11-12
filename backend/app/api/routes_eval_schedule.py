# -*- coding: utf-8 -*-
"""评测中心分级调度 API（M18 V3.0 持续评测治理）。

数据集按分层（smoke/core/full）绑定手动/每日/每周/变更调度：
- 调度只负责「到期触发 Run」；绝不自动修改任何决策/退款。
- 绑定非 APPROVED Prompt 直接拒绝（人工批准门不可绕）。
- 幂等：unique(suite_id, tier)；触发后 last_run_id 记录证据链。
"""
import json
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import ROLE_LEADER, ROLE_SUPERVISOR, require_min_role
from app.domain.models import EvalSchedule, EvalSuite, User
from app.infrastructure.audit import write_event
from app.infrastructure.db import get_db
from app.infrastructure.redis_client import STREAM_EVAL_RUN, xadd
from app.services.eval_center import (
    ack_alert,
    create_run,
    create_schedule,
    due_schedules,
    get_schedule,
    list_alerts,
    list_schedules,
    mark_schedule_fired,
    set_schedule_enabled,
)

router = APIRouter(prefix="/evaluation-center/schedules", tags=["evaluation-center"])

EVAL_TIERS = ("smoke", "core", "full", "security", "voice", "intent")
CADENCES = ("manual", "daily", "weekly", "change")


class CreateScheduleBody(BaseModel):
    suite_id: str = Field(min_length=1, max_length=36)
    tier: str = Field(pattern="^(smoke|core|full|security|voice|intent)$")
    cadence: str = Field(default="manual", pattern="^(manual|daily|weekly|change)$")
    enabled: bool = Field(default=True)


class UpdateScheduleBody(BaseModel):
    enabled: bool


@router.get("")
def schedule_list(enabled_only: bool = False,
                  user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                  db: Session = Depends(get_db)):
    return list_schedules(db, enabled_only=enabled_only)


@router.post("", status_code=201)
def schedule_create(body: CreateScheduleBody,
                    user: User = Depends(require_min_role(ROLE_LEADER)),
                    db: Session = Depends(get_db)):
    try:
        item = create_schedule(db, suite_id=body.suite_id, tier=body.tier,
                               cadence=body.cadence, enabled=body.enabled,
                               owner_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="数据集不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    write_event(db, actor_id=str(user.id), case_id=None,
                action="SCHEDULE_CREATED",
                payload={"schedule_id": item["id"], "suite_id": body.suite_id,
                         "tier": body.tier, "cadence": body.cadence,
                         "enabled": body.enabled})
    db.commit()
    return {"item": item}


# 注意：/alerts、/alerts/{alert_id}/ack 为静态路径，必须在 /{schedule_id} 之前注册，
# 否则 /alerts 会被 {schedule_id} 误吞（同 /reports/compare 教训）。

# ---- V2.0 定时评测失败告警（RUN_FAILED；只告警，绝不自动改评测/退款）----

@router.get("/alerts")
def alerts_list(acked: bool | None = None,
                user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                db: Session = Depends(get_db)):
    """定时评测失败告警列表（acked 可过滤已处理/未处理）。"""
    return list_alerts(db, acked=acked)


@router.post("/alerts/{alert_id}/ack")
def alerts_ack(alert_id: str,
               user: User = Depends(require_min_role(ROLE_LEADER)),
               db: Session = Depends(get_db)):
    """标记一条失败告警已处理（ack 后不再重复提示同一 run）。"""
    try:
        item = ack_alert(db, alert_id, acked_by=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="告警不存在")
    write_event(db, actor_id=str(user.id), case_id=None,
                action="EVAL_ALERT_ACKED",
                payload={"alert_id": alert_id, "run_id": item.get("run_id")})
    db.commit()
    return {"item": item}


@router.get("/{schedule_id}")
def schedule_detail(schedule_id: str,
                    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
                    db: Session = Depends(get_db)):
    item = get_schedule(db, schedule_id)
    if item is None:
        raise HTTPException(status_code=404, detail="调度不存在")
    return {"item": item}


@router.patch("/{schedule_id}")
def schedule_update(schedule_id: str, body: UpdateScheduleBody,
                    user: User = Depends(require_min_role(ROLE_LEADER)),
                    db: Session = Depends(get_db)):
    try:
        item = set_schedule_enabled(db, schedule_id, enabled=body.enabled,
                                    owner_id=str(user.id))
    except KeyError:
        raise HTTPException(status_code=404, detail="调度不存在")
    write_event(db, actor_id=str(user.id), case_id=None,
                action="SCHEDULE_UPDATED",
                payload={"schedule_id": schedule_id, "enabled": body.enabled})
    db.commit()
    return {"item": item}


@router.post("/{schedule_id}/fire", status_code=202)
def schedule_fire(schedule_id: str,
                  user: User = Depends(require_min_role(ROLE_LEADER)),
                  db: Session = Depends(get_db)):
    """手动触发一次调度：创建 tier 级 Run 并入队（追加 last_run 证据链）。"""
    sch = db.get(EvalSchedule, schedule_id)
    if sch is None:
        raise HTTPException(status_code=404, detail="调度不存在")
    if not sch.enabled:
        raise HTTPException(status_code=409, detail="调度已停用，不可触发")
    suite = db.get(EvalSuite, sch.suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    if sch.tier == "intent":
        from app.services import intent_periodic_eval as ipe
        active = ipe.active_intent_run(db)
        if active is not None:
            if active.status == "PENDING":
                ipe.enqueue_intent_run(active)
            return {
                "run_id": active.run_id, "schedule_id": schedule_id,
                "tier": sch.tier, "status": active.status,
                "deduplicated": True,
                "message": "已有意图评测在队列中，本次复用该 Run",
            }
        run = ipe.create_intent_eval_run(
            db, created_by=str(user.id), trigger_source="schedule",
            schedule_id=sch.id, suite=suite,
        )
        run_id = run.run_id
        config = run.config or {}
        target_type = "intent_router"
    else:
        run_id = f"run-{_uuid.uuid4().hex[:12]}"
        config = {"mode": "in_process", "tier": sch.tier, "schedule_id": sch.id}
        run = create_run(db, run_id=run_id, name=f"[调度] {suite.name} {sch.tier}",
                         suite=suite, created_by=str(user.id), config=config,
                         trigger_source="schedule")   # V2.0 来源可追溯
        target_type = "refund_workflow"
    from app.services.eval_center import _next_due
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        mark_schedule_fired(db, schedule_id, run_id=run_id,
                            next_due=_next_due(sch.cadence, now))
    except KeyError:
        pass
    # 入队：worker 按 config.tier 过滤 suite tier_map
    xadd(STREAM_EVAL_RUN, {
        "case_id": run_id, "run_id": run_id, "suite_id": sch.suite_id,
        "limit": "0", "priority": "0", "target_type": target_type,
        "config": json.dumps(config),
    })
    write_event(db, actor_id=str(user.id), case_id=None,
                action="SCHEDULE_FIRED",
                payload={"schedule_id": schedule_id, "run_id": run_id,
                         "suite_id": sch.suite_id, "tier": sch.tier,
                         "cadence": sch.cadence})
    db.commit()
    return {"run_id": run_id, "schedule_id": schedule_id,
            "tier": sch.tier, "status": run.status,
            "message": f"[{sch.tier}] 评测已入队，由 Evaluation Worker 执行"}
