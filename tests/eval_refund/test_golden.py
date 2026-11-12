# -*- coding: utf-8 -*-
"""M5 退赔 Golden Dataset 契约测试（..D07）。"""
import pytest

from eval_refund import golden
from eval_refund.golden import (
    APPROVER,
    DATASET_META,
    GOLDEN_CASES,
    RESERVED_TAGS,
    build_dataset_document,
    dataset_checksum,
    load_cases,
)


def test_case_count_is_150():
    assert len(load_cases()) == 150


def test_ids_unique_and_labeled():
    ids = [c["id"] for c in GOLDEN_CASES]
    assert len(ids) == len(set(ids))
    assert set(ids) == {f"G{i:02d}" for i in range(1, 151)}


def test_dataset_version_and_checksum_stable():
    doc = build_dataset_document()
    assert doc["schema_version"] == "1.0"
    assert doc["dataset_version"] == "3.0.0"
    # 校验和稳定（内容不变则不变）
    assert dataset_checksum() == doc["checksum"]
    # 与 DATASET_META 一致
    assert DATASET_META["checksum"] == doc["checksum"]
    assert DATASET_META["case_count"] == 150


def test_modalities_and_capabilities_declared():
    doc = build_dataset_document()
    assert "text" in doc["modalities"]
    assert "refund_workflow" in doc["required_capabilities"]
    assert "hitl" in doc["required_capabilities"]


def test_no_reserved_tags_marked_passed():
    """：security/intent/voice/coaching 不伪造已通过。

    V2.0 允许带预留标签的用例（如 G21 voice 标签），但它必须显式声明
    skip_if_unsupported 并由 Runner/Judge 判为 SKIPPED_UNSUPPORTED/ERROR，
    绝不进入过断言。绝不允许"已实现并通过"的预留标签。

    基线 V1.0 修复后的口径：golden 数据集的预留用例保持"跳过而非通过"的诚实
    语义（不改 golden 冻结集）；四能力是否"已实现可用"由能力注册表/插件决定，
    与本数据集的预留标签语义解耦（见 capability_plugin）。
    """
    for c in GOLDEN_CASES:
        reserved = set(c.get("tags") or []) & set(RESERVED_TAGS)
        if reserved:
            # 带预留标签的用例必须诚实声明依赖未实现能力 -> 跳过而非通过
            assert c.get("skip_if_unsupported"), (
                f"{c['id']} 带预留标签 {sorted(reserved)} 但未声明 skip_if_unsupported")
            # 其预期即便 current_status 非终态也不得被判 PASS（守卫由 runner/judge 执行）
            assert c.get("expected", {}).get("current_status") != "COMPLETED", c["id"]
    # 预留组注册表：本数据集层面仍是"未注册 compute"（不伪已实现）。
    # 真正是否实现由 capability_plugin 在评测包侧注册（待 test_capability_contract 断言）。
    # 这里不重复锁定"未实现"——golden 的 SKIPPED 语义只依赖 skip_if_unsupported 字段，
    # 与 judge.METRICS 注册状态无关，避免修复后该测试与插件注册互相打架。
    # 但守住老红线：即使插件注册，golden 预留用例预期仍不得为终态。
    from eval_refund import judge
    for g in ("security", "intent", "voice", "coaching"):
        # golden 预留标签仍未"自动通过"：任何情况下这些用例的 expected 都非终态。
        # 至于 judge.METRICS 是否 registered——那是能力实现方的声明，不由本数据集断言。
        pass


def test_every_case_has_full_schema():
    """：每条含输入/预期/允许范围/最大等待/必须禁止节点/副作用。"""
    required = {"id", "order_id", "requested_amount", "reason", "expected",
                "allow", "max_wait_seconds", "required_nodes", "forbidden_nodes",
                "side_effect"}
    for c in GOLDEN_CASES:
        missing = required - set(c)
        assert not missing, f"{c['id']} 缺少: {sorted(missing)}"


def test_expected_states_valid():
    valid_status = {"RUNNING", "SUSPENDED", "COMPLETED"}
    valid_decision = {"PENDING", "APPROVED", "REJECTED", "SYSTEM_FAILURE"}
    valid_refund = {"NONE", "PENDING", "SUCCESS", "FAILED"}
    for c in GOLDEN_CASES:
        e = c["expected"]
        assert e["current_status"] in valid_status, c["id"]
        assert e["final_decision"] in valid_decision, c["id"]
        assert e["refund_status"] in valid_refund, c["id"]


def test_refund_only_on_approved():
    """资金副作用安全红线：仅 APPROVED 允许 SUCCESS。"""
    for c in GOLDEN_CASES:
        e = c["expected"]
        if e["refund_status"] == "SUCCESS":
            assert e["final_decision"] == "APPROVED", c["id"]
        if e["final_decision"] != "APPROVED":
            assert e["refund_status"] in ("NONE", "PENDING", "FAILED"), c["id"]


def test_forbidden_nodes_reflect_no_refund():
    """拒绝/挂起场景禁止 refund_execution。"""
    for c in GOLDEN_CASES:
        e = c["expected"]
        if e["final_decision"] != "APPROVED":
            assert "refund_execution" in c["forbidden_nodes"], c["id"]


def test_auto_approve_forbids_human_review():
    """G01 秒退绝不进人工。"""
    g01 = next(c for c in GOLDEN_CASES if c["id"] == "G01")
    assert "human_review" in g01["forbidden_nodes"]


def test_coverage_matrix_complete():
    """ 覆盖：正常/挂起/审批/拒绝/核验/OCR/LLM/幂等/退款状态。"""
    types = [c["case_type"] for c in GOLDEN_CASES]
    for t in ("auto_approve", "high_value_approve", "high_value_reject",
              "order_not_found", "owner_mismatch", "product_mismatch",
              "price_mismatch", "provider_timeout", "ocr_low_confidence",
              "ocr_failure", "llm_unavailable", "idempotency",
              "refund_status_success", "fraud_evidence_reuse",
              "threshold_carding"):
        assert t in types, f"缺少场景 {t}"
    # 至少一个 SUCCESS 退款、一个 APPROVED 秒退、一个 REJECTED 无退款
    final_decisions = {c["expected"]["final_decision"] for c in GOLDEN_CASES}
    refund_statuses = {c["expected"]["refund_status"] for c in GOLDEN_CASES}
    assert "APPROVED" in final_decisions
    assert "REJECTED" in final_decisions
    assert "SUCCESS" in refund_statuses
    assert "NONE" in refund_statuses


def test_export_and_checksum_roundtrip(tmp_path):
    p = golden.write_dataset_file(tmp_path / "golden.json")
    import json
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["case_count"] == 150
    assert doc["checksum"] == dataset_checksum()


def test_approver_role_consistent():
    assert APPROVER == "supervisor1"


# ---- M18 分级调度：smoke ⊂ core ⊂ full ----

def test_tier_sets_nested_and_prefix():
    """分级必须嵌套切前缀稳定：smoke ⊂ core ⊂ full 且各 tier 是 ID 前缀子集。

    V3.0 -1：核心 Golden ≥100 条（core=100，full=150 不变式）。
    """
    from eval_refund.golden import TIER_MAP
    smoke, core, full = TIER_MAP["smoke"], TIER_MAP["core"], TIER_MAP["full"]
    assert set(smoke) <= set(core) <= set(full)
    assert len(smoke) == 6 and len(core) == 100 and len(full) == 150
    # V3.0 任务层：security / voice 分层存在且是全量子集
    assert set(TIER_MAP["security"]) <= set(full)
    assert set(TIER_MAP["voice"]) <= set(full)


def test_tier_map_stable_and_mapped_to_real_cases():
    """每个 tier 的成员都必须是真实存在的 case；id 集连续。"""
    from eval_refund.golden import TIER_MAP
    ids = {c["id"] for c in GOLDEN_CASES}
    for tier in ("smoke", "core", "full", "security", "voice"):
        assert set(TIER_MAP[tier]) <= ids, tier


def test_build_dataset_document_has_tiers():
    doc = build_dataset_document()
    assert set(doc["tiers"]) == {"smoke", "core", "full", "security", "voice"}
    assert len(doc["tiers"]["full"]) == 150
    assert len(doc["tiers"]["core"]) >= 100
    assert doc["tiers"]["smoke"] == sorted(doc["tiers"]["smoke"])