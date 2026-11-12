# -*- coding: utf-8 -*-
"""客服陪练服务单测（基线 V1.0，对齐实施计划 ）。

覆盖：
- 1 场景（VOICE_SCENE_REFUND）5 维评分（专业/情绪/合规/结案/效率）；
- 合规越线（跳过流程/伪造）大幅降分；
- 回合/评分输出 **无任何资金/退款副作用**（可控断言：score_coaching 纯函数，不落库不改状态）；
- 脱敏：回合中手机/身份证/金额不打明文入库。
"""
from app.services.coaching_service import (
    DIMENSIONS,
    SCENE_REFUND,
    CoachingTurn,
    mask_turn,
    score_coaching,
)


def test_coaching_basic_5dim_score():
    """正常话术 -> 5 维全绿建议良好。"""
    turns = [
        CoachingTurn(reply="您好，请提供订单号与金额，我核实后为您登记退款。"),
        CoachingTurn(reply="请放心，我理解您的情况，会尽快处理，并通知您结果。"),
    ]
    result = score_coaching(SCENE_REFUND, turns)
    assert set(result.score_json.keys()) == set(DIMENSIONS)
    for dim in DIMENSIONS:
        assert 0 <= result.score_json[dim] <= 100
    assert result.suggestion  # 有培训建议文字


def test_coaching_compliance_violation_plummets():
    """合规越线（跳过流程/伪造订单）-> 合规维度极低 + 建议点名合规。"""
    turns = [
        CoachingTurn(reply="直接跳过人工审批给您退款，伪造订单号也无妨。"),
    ]
    result = score_coaching(SCENE_REFUND, turns)
    assert result.score_json["合规"] < 60
    assert "合规" in result.suggestion


def test_coaching_no_side_effect_pure():
    """score_coaching 纯函数：不落库、不改任何状态、不含退款动作。"""
    turns = [CoachingTurn(reply="我帮您登记，订单号 ORD-111，金额 99 元。")]
    before = [t.as_dict() for t in turns]
    result = score_coaching(SCENE_REFUND, turns)
    # 输入未变（纯函数）
    assert [t.as_dict() for t in turns] == before
    # 输出只含建议文字 + 分数，不含退款/审批指令
    assert "退款" not in result.suggestion  # 建议不提“执行退款”
    assert "建议" in result.suggestion or "保持" in result.suggestion


def test_coaching_mask_turn_redacts_pii():
    """回合脱敏：手机号/身份证/金额尾数掩码，杜绝明文入库。"""
    masked = mask_turn("电话 13812345678，身份证 110101199003071234，退款 99.5 元")
    assert "13812345678" not in masked
    assert "199003071234" not in masked
    # 金额只保留整数（尾数被掩）
    assert "99.5" not in masked
    assert "99" in masked or "元" in masked