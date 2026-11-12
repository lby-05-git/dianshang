# -*- coding: utf-8 -*-
"""语音产品化能力端点（V2.0 -5 / 计划 新增 routes_voice_product.py）。

覆盖（路径契约见计划 接口表）：
  POST /api/v1/voice/ws/recovery      断线重连恢复（同 session 复用 / 新会话挂入）
  GET  /api/v1/voice/session/{id}     会话状态快照 + 全程 Trace（恢复/审计对账）
  GET  /api/v1/voice/metrics          容量基线看板（主管+）：P99 时延/错误率/并发峰值/Drop/占用率
  GET  /api/v1/voice/coaching/scenarios    陪练多场景目录
  GET  /api/v1/voice/coaching/replay/{id}  陪练回放（脱敏回合 + 评分 + 解释 + 建议）

红线：全部端点只读/观测（陪练仅建议文字）——绝不触发退款/资金/审批/状态变更；
陪练线索隔离：测评环境无法访问真实用户数据/退款凭据（仅构造话术+只读评分）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import ROLE_SUPERVISOR, get_current_user, require_min_role
from app.domain.models import CoachingSession, User
from app.infrastructure.db import get_db
from app.services import voice_service as vs
from app.services import voice_telemetry as vt
from app.services.coaching_service import build_replay, list_scenarios
from app.services.voice_service import VoiceCapacityExceeded

router = APIRouter(prefix="/voice", tags=["voice-product"])


# ---- 断线重连恢复 ----

@router.post("/ws/recovery", status_code=200)
def ws_recovery(
    session_id: str = Form(..., min_length=1, max_length=64),
    user: User = Depends(get_current_user),
):
    """断线重连恢复：同 session 命中 -> recover()（reconnects+=1 复用）；
    未命中 -> 新建会话挂入注册表（返回实际 session_id）。容量满则 429。

    只做注册/计数（审计普通对象），绝不触碰退款/决策动作。"""
    try:
        svc = vs.voice_manager.reconnect(session_id)
    except VoiceCapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "capacity_full",
                "active": exc.active,
                "max_concurrent": exc.max_concurrent,
            },
        )
    return {
        "session_id": svc.session_id,
        "state": svc.state,
        "reconnects": svc.reconnects,
        "weak_signal": svc.weak_signal,
    }


# ---- 会话状态快照 + Trace（恢复/审计对账）----

@router.get("/session/{session_id}")
def voice_session_snapshot(
    session_id: str,
    user: User = Depends(get_current_user),
):
    """会话状态快照 + 全程 Trace（只读，供断线恢复与审计对账）。"""
    svc = vs.voice_manager.get(session_id)
    if svc is None:
        raise HTTPException(status_code=404,
                            detail="会话不存在或已回收（如需审计请查会话记录表）")
    return svc.snapshot()


# ---- 容量基线看板（主管+）----

@router.get("/metrics")
def voice_metrics(
    user: User = Depends(require_min_role(ROLE_SUPERVISOR)),
    db: Session = Depends(get_db),
):
    """容量基线看板：P99 时延 / 错误率 / 并发峰值 / Drop 数 + 当前占用率。

    纯读聚合（voice_metrics 观测表 + 注册表占用），无任何业务副作用。"""
    return vt.compute_baseline(db, session_manager=vs.voice_manager)


# ---- 陪练多场景目录 / 回放（只读）----

@router.get("/coaching/scenarios")
def coaching_scenarios(
    user: User = Depends(get_current_user),
):
    """陪练场景目录（4 场景 × 难度/情绪枚举），供前端出题与校验。"""
    return {"scenarios": list_scenarios()}


@router.get("/coaching/replay/{coaching_session_id}")
def coaching_replay(
    coaching_session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """陪练回放：脱敏回合 + 5 维评分 + 每维解释 + 培训建议（只读，无副作用）。

    turns 落库前已脱敏，此处只回放，验证 eval 陪练永不接触真实资金/数据。"""
    row = db.get(CoachingSession, coaching_session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="陪练会话不存在")
    return build_replay(row)