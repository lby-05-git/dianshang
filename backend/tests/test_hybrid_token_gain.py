# -*- coding: utf-8 -*-
"""V3.0 V3-4 混合路由 Token 对比（LLM vs 混合，降低 ≥40% 实测）。

覆盖：
- 内置抽样语料同口径实测：混合路由相对纯 LLM Token 节省 ≥40%（meets_target=True）；
- 可注入确定性计费器（token_fee）验证精确算术（rule 高置信命中 -> 0 LLM token）；
- 空语料/未测得 -> 如实 None / meets False（不假报达标）；
- observability 看板形状 + 诚实 note。
"""
import pytest

from app.services import intent_router as ir
from app.services.observability import intent_hybrid_token_report


def test_default_corpus_token_gain_meets_target():
    """内置抽样语料：混合路由比纯 LLM 省 ≥40% Token（同口径 estimate 实测）。"""
    r = intent_hybrid_token_report()
    assert r["corpus_n"] >= 10
    assert r["pure_llm_tokens"] > 0
    assert r["hybrid_tokens"] < r["pure_llm_tokens"]
    assert r["token_gain"] >= ir.TOKEN_GAIN_TARGET         # ≥0.40
    assert r["meets_target"] is True
    assert "honest_note" in r and r["measurement"]


def test_deterministic_with_injected_token_fee():
    """注入确定性计费器：高置信规则命中 -> 0 LLM token，增益精确为 1.0。"""
    calls = {"n": 0}

    def fee(prompt: str) -> int:                       # 每个提示计 100 token（可复现）
        calls["n"] += 1
        return 100

    corpus = [
        "我要申请退款退货退款",        # refund 多词命中 -> HIGH，不触发 LLM
        "物流到哪了查单进度订单状态",    # query 多词命中 -> 不触发 LLM
    ]
    r = ir.measure_hybrid_token_gain(corpus, token_fee=fee)
    assert r["pure_llm_tokens"] == 200     # 2 条纯 LLM
    assert r["hybrid_tokens"] == 0
    assert r["token_gain"] == 1.0
    assert r["meets_target"] is True


def test_weak_hit_still_counts_llm():
    """弱命中（confidence < 0.85）仍走 LLM：混合 token > 0，诚实计入。"""
    calls = {"n": 0}

    def fee(prompt: str) -> int:
        calls["n"] += 1
        return 100

    r = ir.measure_hybrid_token_gain(["退钱"], token_fee=fee)   # 单词弱命中(<0.85)
    assert r["hybrid_tokens"] == 100 and r["pure_llm_tokens"] == 100
    assert r["token_gain"] == 0.0
    assert r["meets_target"] is False


def test_empty_corpus_honest_not_measured():
    r = ir.measure_hybrid_token_gain([])
    assert r["corpus_n"] == 0
    assert r["pure_llm_tokens"] == 0
    assert r["token_gain"] is None
    assert r["meets_target"] is False          # 未测得绝不通过


def test_report_shape_and_custom_corpus():
    r = intent_hybrid_token_report(["我要投诉举报12315曝光", "转人工找真人客服"])
    assert r["corpus_n"] == 2
    assert r["target"] == ir.TOKEN_GAIN_TARGET
    assert r["hybrid_tokens"] == 0            # 两条均多词高置信 -> 0 LLM
    assert r["token_gain"] == 1.0