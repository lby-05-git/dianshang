# -*- coding: utf-8 -*-
"""SLA 时钟（D4）：挂起案例启动倒计时，到点提醒/升级，但**永不自动退款**。

- 挂起落库（runner._mark_suspended）时写 sla_deadline_at；
- 该模块只做「判定状态 + 写日志/事件」，不触碰退款；
- 提醒/升级标志由懒扫描（查询时顺带更新）或定时器驱动。
"""
from datetime import datetime, timedelta, timezone

from app.domain.models import RefundCase


def compute_sla_deadline(*, remind_hr: int = 4, escalate_hr: int = 24,
                         started_at: datetime | None = None) -> datetime:
    """从起点推算 SLA 截止时间（挂起那一刻起 escalate_hr 小时）。"""
    base = started_at or datetime.now(timezone.utc)
    return base + timedelta(hours=escalate_hr)


def sla_state(deadline_at: datetime | None, now: datetime | None = None,
              remind_hr: int = 4) -> str:
    """返回当前 SLA 状态：'ok'（未到期）/'remind'（进入提醒区间）/'overdue'（超时）。

    remind 区间 = 距离截止还剩 remind_hr 小时内；overdue = 已过截止。
    仅用于显示与通知，绝不触发自动退款。
    """
    if deadline_at is None:
        return "ok"
    now_ = now or datetime.now(timezone.utc)
    if now_ >= deadline_at:
        return "overdue"
    if now_ >= deadline_at - timedelta(hours=remind_hr):
        return "remind"
    return "ok"


def is_sla_overdue(case: RefundCase, now: datetime | None = None) -> bool:
    """是否超时（用于展示告警/通知）。"""
    return sla_state(case.sla_deadline_at, now) == "overdue"