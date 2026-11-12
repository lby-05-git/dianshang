# -*- coding: utf-8 -*-
"""大屏接口（docs/06 ）：汇总统计 + SSE 实时推送 + 监控大盘数据。"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from jose import JWTError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.schemas import DashboardStats
from app.core.security import decode_access_token
from app.domain.models import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    AgentExecutionLog,
    RefundCase,
    User,
)
from app.infrastructure.db import get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStats)
def stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 5.2：正常业务看板默认排除评测数据（eval_run_id 打标）
    total = db.query(RefundCase).filter(RefundCase.non_eval()).count()
    running = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        current_status=CASE_STATUS_RUNNING).count()
    suspended = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        current_status=CASE_STATUS_SUSPENDED).count()
    completed = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        current_status=CASE_STATUS_COMPLETED).count()
    return DashboardStats(total=total, running=running, suspended=suspended, completed=completed)


@router.get("/monitor")
def monitor(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """大屏监控数据接口：返回实时大盘数据、Agent 聚合统计、风险分布、趋势等。"""
    total = db.query(RefundCase).filter(RefundCase.non_eval()).count()

    # 今日工单数
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = db.query(RefundCase).filter(
        RefundCase.non_eval(), RefundCase.created_at >= today_start).count()

    # 昨日工单数（用于对比）
    yesterday_start = today_start - timedelta(days=1)
    yesterday_count = db.query(RefundCase).filter(
        RefundCase.non_eval(),
        RefundCase.created_at >= yesterday_start,
        RefundCase.created_at < today_start,
    ).count()

    # 状态分布
    running = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        current_status=CASE_STATUS_RUNNING).count()
    suspended = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        current_status=CASE_STATUS_SUSPENDED).count()
    completed = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        current_status=CASE_STATUS_COMPLETED).count()

    # 决策分布
    approved = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        final_decision="APPROVED").count()
    rejected = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        final_decision="REJECTED").count()
    system_failure = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        final_decision="SYSTEM_FAILURE").count()
    pending = db.query(RefundCase).filter(RefundCase.non_eval()).filter_by(
        final_decision="PENDING").count()

    # Agent 平均耗时（毫秒）
    agent_avg = {}
    for name in ("intake", "ocr", "fraud", "sentiment", "amount_decision", "refund_execution"):
        rows = db.query(AgentExecutionLog.duration_ms).filter(
            AgentExecutionLog.agent_name == name,
            AgentExecutionLog.duration_ms.isnot(None),
        ).all()
        vals = [r[0] for r in rows if r[0] is not None]
        agent_avg[name] = round(sum(vals) / len(vals), 1) if vals else 0

    # Agent 成功率
    agent_success = {}
    for name in ("intake", "ocr", "fraud", "sentiment", "amount_decision", "refund_execution"):
        total_logs = db.query(AgentExecutionLog).filter(
            AgentExecutionLog.agent_name == name,
        ).count()
        success_logs = db.query(AgentExecutionLog).filter(
            AgentExecutionLog.agent_name == name,
            AgentExecutionLog.status == "SUCCESS",
        ).count()
        agent_success[name] = round(success_logs / total_logs * 100, 1) if total_logs else 100.0

    # 最近工单（取前 20 条；排除评测打标）
    recent = db.query(RefundCase).filter(RefundCase.non_eval()).order_by(
        RefundCase.created_at.desc()).limit(20).all()
    recent_cases = [
        {
            "case_id": c.id[:8],
            "amount": float(c.requested_amount),
            "status": c.current_status,
            "decision": c.final_decision,
            "fraud_score": c.fraud_score,
            "sentiment": c.sentiment_level,
            "node": c.current_graph_node,
            "created_at": c.created_at.strftime("%m-%d %H:%M") if c.created_at else "",
        }
        for c in recent
    ]

    # 风险等级分布
    high_risk = db.query(RefundCase).filter(
        RefundCase.non_eval(),
        RefundCase.fraud_score.isnot(None),
        RefundCase.fraud_score >= 60,
    ).count()
    mid_risk = db.query(RefundCase).filter(
        RefundCase.non_eval(),
        RefundCase.fraud_score.isnot(None),
        RefundCase.fraud_score.between(21, 59),
    ).count()
    low_risk = db.query(RefundCase).filter(
        RefundCase.non_eval(),
        RefundCase.fraud_score.isnot(None),
        RefundCase.fraud_score <= 20,
    ).count()

    # 自动 vs 人工决策比例
    auto_count = db.query(RefundCase).filter(
        RefundCase.non_eval(),
        RefundCase.current_graph_node == "finalize",
        RefundCase.final_decision == "APPROVED",
    ).count()
    manual_count = db.query(RefundCase).filter(
        RefundCase.non_eval(),
        RefundCase.current_graph_node == "human_review",
    ).count()

    # 每小时工单趋势（近 24h）
    hourly_labels = []
    hourly_counts = []
    now = datetime.now()
    for i in range(23, -1, -1):
        h_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=i)
        h_end = h_start + timedelta(hours=1)
        cnt = db.query(RefundCase).filter(
            RefundCase.non_eval(),
            RefundCase.created_at >= h_start,
            RefundCase.created_at < h_end,
        ).count()
        hourly_labels.append(h_start.strftime("%H:00"))
        hourly_counts.append(cnt)

    # 每日工单趋势（近 7 天）
    daily_labels = []
    daily_counts = []
    for i in range(6, -1, -1):
        d_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        d_end = d_start + timedelta(days=1)
        cnt = db.query(RefundCase).filter(
            RefundCase.non_eval(),
            RefundCase.created_at >= d_start,
            RefundCase.created_at < d_end,
        ).count()
        daily_labels.append(d_start.strftime("%m-%d"))
        daily_counts.append(cnt)

    return {
        "total": total,
        "today_count": today_count,
        "yesterday_count": yesterday_count,
        "status_dist": {"RUNNING": running, "SUSPENDED": suspended, "COMPLETED": completed},
        "decision_dist": {"APPROVED": approved, "REJECTED": rejected, "SYSTEM_FAILURE": system_failure, "PENDING": pending},
        "risk_dist": {"HIGH": high_risk, "MEDIUM": mid_risk, "LOW": low_risk},
        "agent_avg_ms": agent_avg,
        "agent_success_pct": agent_success,
        "auto_vs_manual": {"auto": auto_count, "manual": manual_count},
        "recent_cases": recent_cases,
        "trend": {
            "hourly": {"labels": hourly_labels, "counts": hourly_counts},
            "daily": {"labels": daily_labels, "counts": daily_counts},
        },
        "refresh_interval": 5,
    }


@router.get("/stream")
async def stream(request: Request, token: str | None = None, db: Session = Depends(get_db)):
    """SSE 实时推送。"""
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        token = auth.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="missing token")
    try:
        payload = decode_access_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    user = db.get(User, payload.get("sub"))
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")

    async def event_gen():
        yield ": connected\n\n"
        last_ts = datetime.now()
        while True:
            rows = (
                db.query(RefundCase)
                .filter(RefundCase.non_eval(), RefundCase.updated_at > last_ts)
                .order_by(RefundCase.updated_at)
                .all()
            )
            for r in rows:
                payload = {
                    "case_id": r.id,
                    "current_status": r.current_status,
                    "final_decision": r.final_decision,
                    "current_graph_node": r.current_graph_node,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                yield f"event: case_update\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_ts = max(last_ts, r.updated_at)
            await asyncio.sleep(1)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})