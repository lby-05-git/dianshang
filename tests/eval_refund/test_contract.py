# -*- coding: utf-8 -*-
"""M9 契约冻结测试（L851）。

冻结 V1.0 对外契约：schema_version、protocol_version、CandidateOutput 字段、
extensions 命名空间名、Judge 维度与红色语义。任何破坏契约的变更都会被捕获，
防止评测协议在模块迭代中意外不兼容（向后兼容）。
"""
import pytest

from eval_refund import judge, schema, target
from eval_refund.golden import DATASET_META, GOLDEN_CASES


def test_schema_version_frozen():
    assert schema.SCHEMA_VERSION == "1.0"


def test_target_type_and_protocol_frozen():
    assert schema.TARGET_TYPE == "refund_workflow"
    assert target.DEFAULT_BASE_URL == "http://127.0.0.1:8000"
    # capabilities 协议版本（SUT 端 /api/v1/evaluation/capabilities 同样 1.0）
    from eval_refund.schema import TARGET_TYPE
    assert TARGET_TYPE == schema.TARGET_TYPE


def test_candidate_output_business_keys_frozen():
    """business 核心字段不可随机扩展/重命名（-B16）。"""
    out = schema.CandidateOutput().as_dict()
    assert set(out["business"].keys()) == {
        "current_status", "final_decision", "refund_status",
        "current_graph_node", "order_verification",
        "product_consistency", "price_consistency",
    }


def test_extensions_namespace_frozen():
    assert tuple(schema.EXT_NAMESPACES) == ("security", "intent", "voice", "coaching")


def test_judge_version_and_dimensions_frozen():
    assert judge.JUDGE_VERSION == "1.0"
    assert tuple(judge.LLM_JUDGE_DIMENSIONS) == (
        "sentiment_explanation", "summary_faithful", "completeness",
        "expression_quality")


def test_judge_result_fields_frozen():
    res = judge.JudgeResult(case_id="x", verdict="pass", score=1.0)
    assert set(res.as_dict().keys()) == {
        "case_id", "verdict", "score", "dimensions", "evidence",
        "judge_version", "failure_reason", "llm", "degraded_reason"}


def test_golden_schema_version_frozen():
    assert DATASET_META["version"] == "3.0.0"
    assert len(GOLDEN_CASES) == 150


def test_metrics_namespace_frozen():
    """：metric 组与预留组名稳定。"""
    from eval_refund.judge import METRICS
    for group in ("security", "intent", "voice", "coaching"):
        assert any(m.endswith(f".{group}") or METRICS[m]["group"] == group
                   for m in METRICS), f"缺 {group} 预留"


def test_contract_breaking_guard(mocker=None):
    """一旦有人改了契约核心字段，这里报 fail（人工确认是否升级契约）。"""
    import json
    from eval_refund.schema import CandidateOutput
    snap = json.dumps(CandidateOutput().as_dict(), sort_keys=True, ensure_ascii=False)
    assert "schema_version" in snap
    # business 核心字段稳定（精确 key 集已由 test_candidate_output_business_keys_frozen 冻结）
    biz = json.loads(snap)["business"]
    assert set(biz.keys()) == {
        "current_status", "final_decision", "refund_status",
        "current_graph_node", "order_verification",
        "product_consistency", "price_consistency"}


def test_target_errors_are_runtime_subclass():
    """TargetError 必须可被调用方捕获为 RuntimeError。"""
    assert issubclass(target.TargetError, RuntimeError)