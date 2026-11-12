# -*- coding: utf-8 -*-
"""三态状态机 + 决策终态分离（docs/06 ）。

核心一致性约束：
  1. COMPLETED ⟺ final_decision ≠ PENDING（流程走完必有业务结论）；
  2. 终态（APPROVED/REJECTED）禁止再流转；SYSTEM_FAILURE 允许人工重试后转终态；
  3. 非法状态转换抛 StateViolation，绝不静默覆盖。
"""
from app.domain.models import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    DECISION_SYSTEM_FAILURE,
)

# 工单状态机：当前状态 -> 允许的下一状态
STATUS_TRANSITIONS: dict[str, set[str]] = {
    CASE_STATUS_RUNNING: {CASE_STATUS_SUSPENDED, CASE_STATUS_COMPLETED},
    CASE_STATUS_SUSPENDED: {CASE_STATUS_RUNNING, CASE_STATUS_COMPLETED},
    CASE_STATUS_COMPLETED: set(),
}

# 决策终态机：当前决策 -> 允许的下一决策
DECISION_TRANSITIONS: dict[str, set[str]] = {
    DECISION_PENDING: {DECISION_APPROVED, DECISION_REJECTED, DECISION_SYSTEM_FAILURE},
    DECISION_SYSTEM_FAILURE: {DECISION_APPROVED, DECISION_REJECTED},  # 人工重试后
    DECISION_APPROVED: set(),
    DECISION_REJECTED: set(),
}


class StateViolation(ValueError):
    """非法状态/决策转换，对应接口返回 409。"""


def transition_case(
    *,
    current_status: str,
    current_decision: str,
    new_status: str | None = None,
    new_decision: str | None = None,
) -> tuple[str, str]:
    """统一状态流转入口：校验 + 返回 (new_status, new_decision)。

    - 已完成案件禁止任何流转；
    - new_status 与 new_decision 可以只改一个（另一个保持不变）；
    - 一致性约束在调用层落库时由 WHERE 条件（乐观锁）兜底。
    """
    if current_status not in STATUS_TRANSITIONS or current_decision not in DECISION_TRANSITIONS:
        raise StateViolation(f"未知状态/决策: {current_status}/{current_decision}")

    if current_status == CASE_STATUS_COMPLETED and (new_status or new_decision):
        # 唯一例外：SYSTEM_FAILURE（退款失败）允许人工重试后改写决策终态
        if new_status is None and current_decision == DECISION_SYSTEM_FAILURE:
            pass
        else:
            raise StateViolation("案件已完成，禁止再次流转")

    ns = new_status or current_status
    nd = new_decision or current_decision

    if ns != current_status and ns not in STATUS_TRANSITIONS[current_status]:
        raise StateViolation(f"非法状态转换: {current_status} -> {ns}")
    if nd != current_decision and nd not in DECISION_TRANSITIONS[current_decision]:
        raise StateViolation(f"非法决策转换: {current_decision} -> {nd}")

    # 一致性约束
    if ns == CASE_STATUS_COMPLETED and nd == DECISION_PENDING:
        raise StateViolation("状态进入 COMPLETED 时 final_decision 不能为 PENDING")
    if nd != DECISION_PENDING and ns != CASE_STATUS_COMPLETED:
        raise StateViolation(f"final_decision 写入终态 {nd} 时 current_status 必须为 COMPLETED")

    return ns, nd


def is_terminal_decision(decision: str) -> bool:
    return decision in (DECISION_APPROVED, DECISION_REJECTED, DECISION_SYSTEM_FAILURE)
