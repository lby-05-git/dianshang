# -*- coding: utf-8 -*-
"""Phase 2 验证：状态机合法/非法转换、一致性约束、终态锁定。"""
import pytest

from app.domain.state_machine import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    DECISION_SYSTEM_FAILURE,
    StateViolation,
    transition_case,
)


def test_normal_flow_suspend_then_approve():
    # RUNNING -> SUSPENDED（挂起，决策仍 PENDING）
    s, d = transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                           new_status=CASE_STATUS_SUSPENDED)
    assert (s, d) == (CASE_STATUS_SUSPENDED, DECISION_PENDING)
    # SUSPENDED -> COMPLETED + APPROVED（主管审批通过）
    s, d = transition_case(current_status=s, current_decision=d,
                           new_status=CASE_STATUS_COMPLETED, new_decision=DECISION_APPROVED)
    assert (s, d) == (CASE_STATUS_COMPLETED, DECISION_APPROVED)


def test_auto_complete_scenario2():
    # 128 元自动秒退：RUNNING -> COMPLETED + APPROVED（单步）
    s, d = transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                           new_status=CASE_STATUS_COMPLETED, new_decision=DECISION_APPROVED)
    assert (s, d) == (CASE_STATUS_COMPLETED, DECISION_APPROVED)


def test_illegal_transition_completed_cannot_move():
    s, d = transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                           new_status=CASE_STATUS_COMPLETED, new_decision=DECISION_APPROVED)
    with pytest.raises(StateViolation):
        transition_case(current_status=s, current_decision=d, new_status=CASE_STATUS_RUNNING)


def test_illegal_decision_transition_from_terminal():
    # APPROVED 终态 -> REJECTED 非法（决策机 APPROVED 无出边）
    with pytest.raises(StateViolation):
        transition_case(current_status=CASE_STATUS_COMPLETED, current_decision=DECISION_APPROVED,
                        new_decision=DECISION_REJECTED)


def test_consistency_completed_requires_terminal_decision():
    with pytest.raises(StateViolation):
        transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                        new_status=CASE_STATUS_COMPLETED)  # 决策仍是 PENDING


def test_consistency_terminal_decision_requires_completed():
    with pytest.raises(StateViolation):
        transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                        new_decision=DECISION_APPROVED)  # 状态不是 COMPLETED


def test_system_failure_then_retry_to_approved():
    # 退款失败 -> SYSTEM_FAILURE（RUNNING 状态下写入？不：终态必须 COMPLETED）
    s, d = transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                           new_status=CASE_STATUS_COMPLETED, new_decision=DECISION_SYSTEM_FAILURE)
    # 人工重试退款 -> APPROVED（SYSTEM_FAILURE -> APPROVED 合法，状态保持 COMPLETED）
    s2, d2 = transition_case(current_status=s, current_decision=d, new_decision=DECISION_APPROVED)
    assert (s2, d2) == (CASE_STATUS_COMPLETED, DECISION_APPROVED)


def test_rejected_is_terminal():
    s, d = transition_case(current_status=CASE_STATUS_RUNNING, current_decision=DECISION_PENDING,
                           new_status=CASE_STATUS_COMPLETED, new_decision=DECISION_REJECTED)
    with pytest.raises(StateViolation):
        transition_case(current_status=s, current_decision=d, new_decision=DECISION_APPROVED)
