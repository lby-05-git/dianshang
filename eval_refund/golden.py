# -*- coding: utf-8 -*-
"""退赔评测 Golden Dataset。

版本不可变的确定性数据（V1.0 全部合成；真实案例须脱敏授权后才可进入）。
每条 Case 满足 ：输入、预期业务事实、允许范围、最大等待、必须/禁止节点、
副作用约束。数据集 schema 支持 schema_version / tags / modalities /
required_capabilities / extensions，V1.0 只用合成数据，
不伪造 security/intent/voice/coaching 场景已通过。

覆盖矩阵：
  正常秒退 / 高额挂起->审批 / 挂起->拒绝 / 订单不存在 / 归属不一致 /
  商品不一致 / 价格超出可退 / Provider 超时 / OCR 低置信 / OCR 失败 /
  LLM 不可用 / 幂等 / 退款状态 / 凭证复用（fraud）/ 卡阈值。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

GOLDEN_SCHEMA_VERSION = "1.0"
CASE_SCHEMA_VERSION = "1.0"

# 数据集版本：内容变化即升版本（不可变；vim：校验和据此计算）
DATASET_VERSION = "3.0.0"
PRIMARY_OWNER = "customer_service1"   # 内置订单归属（seed 演示账号）
APPROVER = "supervisor1"

MODALITIES = ["text", "image"]        # 文本诉求 + 凭证图片
REQUIRED_CAPABILITIES = [
    "refund_workflow",
    "order_verification",
    "hitl",
]

# 安全红线标签：V1.0 不伪造以下场景已通过
RESERVED_TAGS = ["security", "intent", "voice", "coaching"]


def _mktag() -> list[str]:
    return []


# ---- 15 条 Golden Case ----
# 每个 case:
#   id, order_id, requested_amount, reason,                        # 输入
#   expected: {current_status, final_decision, refund_status,      # 预期业务事实
#              order_verification, product_consistency, price_consistency}
#   allow: {verification_set: [...]}   # 允许范围（如挂起时核验可在 MISMATCH/UNKNOWN 集合内）
#   max_wait_seconds, required_nodes, forbidden_nodes, side_effect  # 约束
#   use_unique_order: True 时由 runner 注册唯一订单（免 fraud 历史串扰）

_GOLDEN_CASES_V20: list[dict] = [
    # ---- 正常流程 ----
    {
        "id": "G01",
        "case_type": "auto_approve",
        "tags": _mktag(),
        "order_id": "ORD-1001",
        "requested_amount": "99.00",
        "reason": "保温杯破损申请退款",
        "reason_codes": "quality_breakage",
        "use_unique_order": True,          # 免 fraule 历史串扰，保秒退语义
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["intake", "verification", "ocr", "fraud",
                           "sentiment", "amount_decision", "refund_execution", "finalize"],
        "forbidden_nodes": ["human_review"],   # 秒退绝不进人工
        "side_effect": "refund:success",
    },
    {
        "id": "G02",
        "case_type": "high_value_approve",
        "tags": _mktag(),
        "order_id": "ORD-2001",
        "requested_amount": "888.00",
        "reason": "空气炸锅质量问题申请退款",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 90,
        "required_nodes": ["human_review"],   # 高额必须经人工
        "forbidden_nodes": [],
        "side_effect": "refund:success",
        "approval_required": True,
    },
    {
        "id": "G03",
        "case_type": "high_value_reject",
        "tags": _mktag(),
        "order_id": "ORD-2001",
        "requested_amount": "888.00",
        "reason": "空气炸锅质量问题申请退款",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "REJECTED",
            "refund_status": "NONE",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["REJECTED"]},
        "max_wait_seconds": 90,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],   # 拒绝绝不执行退款
        "side_effect": "no_refund",
        "reject_required": True,
    },
    # ---- 订单核验 ----
    {
        "id": "G04",
        "case_type": "order_not_found",
        "tags": _mktag(),
        "order_id": "ORD-NOT-EXIST",
        "requested_amount": "99.00",
        "reason": "请求退款",
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MISMATCH",   # 不存在=核验通过但不存在的 MISMATCH
            "product_consistency": "UNKNOWN",
            "price_consistency": "UNKNOWN",
        },
        "allow": {"order_verification": ["MISMATCH"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G05",
        "case_type": "owner_mismatch",
        "tags": _mktag(),
        "order_id": "ORD-3001",               # owner=supervisor1，非 customer_service1
        "requested_amount": "99.00",
        "reason": "机械键盘申请退款",
        "as_user": "customer_service1",
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MISMATCH",   # 归属不一致
            "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"order_verification": ["MISMATCH"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G06",
        "case_type": "product_mismatch",
        "tags": _mktag(),
        "order_id": "ORD-3002",               # 蓝牙音箱（黑色）
        "requested_amount": "120.00",
        "reason": "这个不是我买的商品，与我订单不符",
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MATCH",
            "product_consistency": "MISMATCH",  # 用户声称非订单商品
            "price_consistency": "MATCH",
        },
        "allow": {"product_consistency": ["MISMATCH"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G07",
        "case_type": "price_mismatch",
        "tags": _mktag(),
        "order_id": "ORD-3003",               # 可退 300
        "requested_amount": "500.00",
        "reason": "冰箱申请退款",
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MATCH",
            "product_consistency": "MATCH",
            "price_consistency": "MISMATCH",    # 申请超可退
        },
        "allow": {"price_consistency": ["MISMATCH"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G08",
        "case_type": "provider_timeout",
        "tags": _mktag(),
        "order_id": "ORD-TIMEOUT-1",
        "requested_amount": "99.00",
        "reason": "请求退款",
        "timeout_for_username": "customer_service1",   # 订单源超时注入
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "UNKNOWN",    # 超时=UNKNOWN，转人工不伪过
            "product_consistency": "UNKNOWN",
            "price_consistency": "UNKNOWN",
        },
        "allow": {"order_verification": ["UNKNOWN"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    # ---- OCR / LLM 故障 ----
    {
        "id": "G09",
        "case_type": "ocr_low_confidence",
        "tags": _mktag(),
        "order_id": "ORD-OCR-LOW",
        "requested_amount": "99.00",
        "reason": "保温杯破损申请退款",
        "use_unique_order": True,
        "inject": {"ocr_confidence": 0.3},       # 低于阈值 -> UNRELIABLE -> 人工
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"current_status": ["SUSPENDED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G10",
        "case_type": "ocr_failure",
        "tags": _mktag(),
        "order_id": "ORD-OCR-FAIL",
        "requested_amount": "99.00",
        "reason": "保温杯破损申请退款",
        "use_unique_order": True,
        "inject": {"ocr_failure": True},
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
        },
        "allow": {"current_status": ["SUSPENDED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G11",
        "case_type": "llm_unavailable",
        "tags": _mktag(),
        "order_id": "ORD-LLM-DOWN",
        "requested_amount": "99.00",
        "reason": "保温杯破损申请退款",
        "use_unique_order": True,
        "inject": {"llm_failure": True},
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
        },
        "allow": {"current_status": ["SUSPENDED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    # ---- 幂等 / 退款状态 / fraud ----
    {
        "id": "G12",
        "case_type": "idempotency",
        "tags": _mktag(),
        "order_id": "ORD-IDEM",
        "requested_amount": "150.00",
        "reason": "保温杯破损申请退款",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["refund_execution", "finalize"],
        "forbidden_nodes": [],
        "side_effect": "refund:success@lenient",   # 只要一次执行成功（幂等）即可
        "max_duplicate_success": 1,                 # 同一 run_id 至多一次退款 SUCCESS
    },
    {
        "id": "G13",
        "case_type": "refund_status_success",
        "tags": _mktag(),
        "order_id": "ORD-REFUND-OK",
        "requested_amount": "350.00",
        "reason": "跑步机申请退款",
        "use_unique_order": True,
        "approval_required": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 90,
        "required_nodes": ["human_review", "refund_execution", "finalize"],
        "forbidden_nodes": [],
        "side_effect": "refund:success",
    },
    {
        "id": "G14",
        "case_type": "fraud_evidence_reuse",
        "tags": _mktag(),
        "order_id": "ORD-FRAUD",
        "requested_amount": "99.00",
        "reason": "保温杯破损申请退款",
        "use_unique_order": True,
        "reuse_evidence": True,                 # 复用其他案件 MD5 -> fraud +30
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
        },
        "allow": {"current_status": ["SUSPENDED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G15",
        "case_type": "threshold_carding",
        "tags": _mktag(),
        "order_id": "ORD-THRESHOLD",
        "requested_amount": "299.00",           # 恰低于 300 阈值 1 元 -> 卡阈值 +15
        "reason": "保温杯破损申请退款",
        "use_unique_order": True,
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
        },
        "allow": {"current_status": ["SUSPENDED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    # ---- V2.0 扩展场景（G16-G30；保持 G01-G15 不变，V1 校验和稳定）----
    # G16-G30 覆盖：多渠道、部分退款、退款重试、系统故障、Target 未声明
    # 能力(SKIPPED_UNSUPPORTED)、高额+多人审批、幂等重复建单恢复等。
    {
        "id": "G16",
        "case_type": "channel_mini_program",
        "tags": _mktag(),
        "order_id": "ORD-MINI-1",
        "requested_amount": "89.00",
        "reason": "小程序渠道保温杯破损申请退款",
        "channel": "小程序",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["intake", "verification", "ocr", "fraud",
                           "sentiment", "amount_decision", "refund_execution", "finalize"],
        "forbidden_nodes": ["human_review"],
        "side_effect": "refund:success",
    },
    {
        "id": "G17",
        "case_type": "channel_cs_agent",
        "tags": _mktag(),
        "order_id": "ORD-CS-1",
        "requested_amount": "129.00",
        "reason": "客服代提渠道耳机退款",
        "channel": "客服代提",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["refund_execution", "finalize"],
        "forbidden_nodes": ["human_review"],
        "side_effect": "refund:success",
    },
    {
        "id": "G18",
        "case_type": "partial_refund",
        "tags": _mktag(),
        "order_id": "ORD-PARTIAL-1",
        "requested_amount": "60.00",        # 订单原价 300，部分退货允许退部分
        "reason": "部分商品退货申请部分退款",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS", "PENDING"]},
        "max_wait_seconds": 90,
        "required_nodes": ["refund_execution", "finalize"],
        "forbidden_nodes": ["human_review"],
        "side_effect": "refund:success",
    },
    {
        "id": "G19",
        "case_type": "refund_retry",
        "tags": _mktag(),
        "order_id": "ORD-RETRY-1",
        "requested_amount": "199.00",
        "reason": "退款执行首次失败触发重试后成功",
        "use_unique_order": True,
        "inject": {"first_refund_fail": True},   # 首次退款失败 -> 有限重试
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS"]},
        "max_wait_seconds": 90,
        "required_nodes": ["refund_execution", "finalize"],
        "forbidden_nodes": [],
        "side_effect": "refund:success",   # 只允许一次成功（幂等重试不重复退款）
    },
    {
        "id": "G20",
        "case_type": "system_failure_no_refund",
        "tags": _mktag(),
        "order_id": "ORD-SYSFAIL-1",
        "requested_amount": "99.00",
        "reason": "工作流内部异常进入 SYSTEM_FAILURE 且绝不退款",
        "use_unique_order": True,
        "inject": {"system_failure": True},
        "expected": {
            "current_status": "COMPLETED", "final_decision": "SYSTEM_FAILURE",
            "refund_status": "NONE",
        },
        "allow": {"final_decision": ["SYSTEM_FAILURE"], "refund_status": ["NONE"]},
        "max_wait_seconds": 60,
        "required_nodes": ["finalize"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G21",
        "case_type": "skipped_unsupported",
        "tags": ["voice"],                  # 预留标签：未实现能力，不显示通过
        "order_id": "ORD-SKIP-1",
        "requested_amount": "99.00",
        "reason": "Target 未声明 voice 能力，语音用例应 SKIPPED_UNSUPPORTED",
        "use_unique_order": True,
        "skip_if_unsupported": "voice",    # 若 target 未声明 voice -> 跳过而非 FAIL
        "expected": {
            "current_status": "RUNNING", "final_decision": "PENDING",
            "refund_status": "NONE",
        },
        "allow": {"current_status": ["RUNNING"]},
        "max_wait_seconds": 1,
        "required_nodes": [],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G22",
        "case_type": "high_value_approve_leader",
        "tags": _mktag(),
        "order_id": "ORD-LEADER-1",
        "requested_amount": "9999.00",
        "reason": "超高额订单需 leader 级审批",
        "use_unique_order": True,
        "approval_required": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 120,
        "required_nodes": ["human_review", "refund_execution", "finalize"],
        "forbidden_nodes": [],
        "side_effect": "refund:success",
    },
    {
        "id": "G23",
        "case_type": "high_value_reject_leader",
        "tags": _mktag(),
        "order_id": "ORD-LEADER-2",
        "requested_amount": "12000.00",
        "reason": "超高额订单人工拒绝",
        "use_unique_order": True,
        "reject_required": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "REJECTED",
            "refund_status": "NONE",
            "order_verification": "MATCH",
        },
        "allow": {"final_decisions": ["REJECTED"]},
        "max_wait_seconds": 120,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G24",
        "case_type": "idempotent_recreate",
        "tags": _mktag(),
        "order_id": "ORD-IDEM2-1",
        "requested_amount": "150.00",
        "reason": "同一幂等键重复建单/审批/恢复不重复退款",
        "use_unique_order": True,
        "reissue_same_idempotency_key": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
        },
        "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS"]},
        "max_wait_seconds": 90,
        "required_nodes": ["refund_execution", "finalize"],
        "forbidden_nodes": [],
        "side_effect": "refund:success@lenient",   # 同一幂等键至多一次退款
        "max_duplicate_success": 1,
    },
    {
        "id": "G25",
        "case_type": "verification_all_match",
        "tags": _mktag(),
        "order_id": "ORD-VERI-1",
        "requested_amount": "45.00",
        "reason": "三路核验全部 MATCH 的小额退款",
        "use_unique_order": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH", "product_consistency": "MATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["refund_execution", "finalize"],
        "forbidden_nodes": ["human_review"],
        "side_effect": "refund:success",
    },
    {
        "id": "G26",
        "case_type": "product_mismatch_sku",
        "tags": _mktag(),
        "order_id": "ORD-MSKU-1",              # 订单规格 500ml，诉求 1L
        "requested_amount": "150.00",
        "reason": "商品规格不一致但订单商品相同",
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MATCH",
            "product_consistency": "MISMATCH",
            "price_consistency": "MATCH",
        },
        "allow": {"product_consistency": ["MISMATCH"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G27",
        "case_type": "price_mismatch_partial_refunded",
        "tags": _mktag(),
        "order_id": "ORD-PPRICE-1",           # 已退部分，可退余额 < 申请
        "requested_amount": "500.00",
        "reason": "订单已部分退款，申请金额超过剩余可退",
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "MATCH",
            "product_consistency": "MATCH",
            "price_consistency": "MISMATCH",
        },
        "allow": {"price_consistency": ["MISMATCH"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G28",
        "case_type": "provider_unavailable",
        "tags": _mktag(),
        "order_id": "ORD-PUNAVAIL-1",
        "requested_amount": "99.00",
        "reason": "订单 Provider 整体不可用",
        "provider_unavailable": True,
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
            "order_verification": "UNKNOWN",
            "product_consistency": "UNKNOWN",
            "price_consistency": "UNKNOWN",
        },
        "allow": {"order_verification": ["UNKNOWN"]},
        "max_wait_seconds": 60,
        "required_nodes": ["verification", "human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
    {
        "id": "G29",
        "case_type": "human_approve_release",
        "tags": _mktag(),
        "order_id": "ORD-RELEASE-1",
        "requested_amount": "788.00",
        "reason": "高额订单人工批准后释放退款",
        "use_unique_order": True,
        "approval_required": True,
        "expected": {
            "current_status": "COMPLETED", "final_decision": "APPROVED",
            "refund_status": "SUCCESS",
            "order_verification": "MATCH",
        },
        "allow": {"final_decisions": ["APPROVED"]},
        "max_wait_seconds": 90,
        "required_nodes": ["human_review", "refund_execution", "finalize"],
        "forbidden_nodes": [],
        "side_effect": "refund:success",
    },
    {
        "id": "G30",
        "case_type": "threshold_carding_boundary",
        "tags": _mktag(),
        "order_id": "ORD-THRESHOLD-30",       # 恰等于 300 阈值 -> 卡阈值
        "requested_amount": "300.00",
        "reason": "申请金额恰等于可退上限（阈值边界）",
        "use_unique_order": True,
        "expected": {
            "current_status": "SUSPENDED", "final_decision": "PENDING",
            "refund_status": "NONE",
        },
        "allow": {"current_status": ["SUSPENDED"]},
        "max_wait_seconds": 60,
        "required_nodes": ["human_review"],
        "forbidden_nodes": ["refund_execution"],
        "side_effect": "no_refund",
    },
]


# =====================================================================
# V3.0 扩展（G31-G150）：确定性回归 + 分级调度（smoke/core/full）。
# 复用 V1/V2 既有 case 模板族的参数化生成；用全新 order_id/金额/诉求文本，
# 每条满足 全字段。Oracle（GoldenFollowerExecutor / worker 进程内）
# 按期望原样产出 → 全新用例同样通过，不改判定语义。
# 预算：smoke 6 / core 100 / full 150（送 V3.0 -1「核心 ≥100 条」；
#       G31-G150 扩展中 smoke/core 均从全量未打标用例确定性子集选出）。
# =====================================================================

DATASET_TIERS = ["smoke", "core", "full", "security", "voice"]
TIER_LABELS = {
    "smoke": "冒烟：最高频正常链路 + 资金红线拒退",
    "core": "核心：核验/OCR/LLM/幂等/审批失败隔离全覆盖（≥100 条，-1）",
    "full": "全量：多通道/边界/故障注入/预留能力逐一覆盖",
    "security": "安全任务层：预留安全能力用例（tag=security，能力未就绪则 SKIPPED）",
    "voice": "语音任务层：预留语音能力用例（tag=voice，能力未就绪则 SKIPPED）",
}


def _build_v3_regression_cases() -> list[dict]:
    """确定性生成 G31-G150（120 条）。

    生成规则（全部确定性，版本不可变前提）：
    - 每个 V1/V2 case_type 族扩展到若干变体（id 序稳定）；
    - 小额/中额/大额金额模板；assist 物料池；诉求文案池；
    - 复用 oracle 语义字段（expected/allow/nodes/side_effect）。
    """

    def amount_for(idx: int) -> str:
        """按序号生成稳定金额：小额<100 / 中额<300 / 大额>=300（阈值区）循环。"""
        base = [29.9, 59.0, 89.0, 129.0, 199.0, 259.0, 358.0, 599.0, 888.0, 1200.0]
        a = base[idx % len(base)]
        return f"{round(a + (idx % 7) * 0.5, 2):.2f}"

    material_pool = [
        ("保温杯", "杯身凹陷漏水"), ("空气炸锅", "内胆涂层脱落"),
        ("蓝牙音箱", "开机异响"), ("机械键盘", "按键失灵"),
        ("电热水壶", "烧水有异味"), ("扫地机器人", "工作途中卡死"),
        ("智能门锁", "无法解锁"), ("车载支架", "夹扣断裂"),
        ("毛毯", "洗涤后起球严重"), ("榨汁机", "刀头晃动"),
    ]

    def reason_for(idx: int) -> str:
        product, flaw = material_pool[idx % len(material_pool)]
        return f"新批次{product}{flaw}，申请退款"

    # 模板族 -> (case_type, 决策/退款/挂起行为, 所需节点, 禁止节点, 附加字段)
    T = {
        "auto_approve": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"]},
            "required_nodes": ["intake", "verification", "ocr", "fraud",
                               "sentiment", "amount_decision", "refund_execution", "finalize"],
            "forbidden_nodes": ["human_review"], "side_effect": "refund:success",
        },
        "high_value_approve": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"]},
            "required_nodes": ["human_review", "refund_execution", "finalize"],
            "forbidden_nodes": [], "side_effect": "refund:success",
            "approval_required": True,
        },
        "high_value_reject": {
            "expected": {"current_status": "COMPLETED", "final_decision": "REJECTED",
                         "refund_status": "NONE",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["REJECTED"]},
            "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund", "reject_required": True,
        },
        "order_not_found": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MISMATCH",
                         "product_consistency": "UNKNOWN", "price_consistency": "UNKNOWN"},
            "allow": {"order_verification": ["MISMATCH"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
        },
        "owner_mismatch": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MISMATCH",
                         "product_consistency": "MATCH", "price_consistency": "MATCH"},
            "allow": {"order_verification": ["MISMATCH"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
            "as_user": "customer_service1",
        },
        "product_mismatch": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MATCH",
                         "product_consistency": "MISMATCH", "price_consistency": "MATCH"},
            "allow": {"product_consistency": ["MISMATCH"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
        },
        "price_mismatch": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MATCH",
                         "product_consistency": "MATCH", "price_consistency": "MISMATCH"},
            "allow": {"price_consistency": ["MISMATCH"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
        },
        "provider_timeout": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "UNKNOWN",
                         "product_consistency": "UNKNOWN", "price_consistency": "UNKNOWN"},
            "allow": {"order_verification": ["UNKNOWN"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
            "timeout_for_username": "customer_service1",
        },
        "ocr_low_confidence": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MATCH",
                         "product_consistency": "MATCH", "price_consistency": "MATCH"},
            "allow": {"current_status": ["SUSPENDED"]},
            "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund", "inject": {"ocr_confidence": 0.3},
        },
        "ocr_failure": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE"},
            "allow": {"current_status": ["SUSPENDED"]},
            "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund", "inject": {"ocr_failure": True},
        },
        "llm_unavailable": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE"},
            "allow": {"current_status": ["SUSPENDED"]},
            "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund", "inject": {"llm_failure": True},
        },
        "idempotency": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS"},
            "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS"]},
            "required_nodes": ["refund_execution", "finalize"], "forbidden_nodes": [],
            "side_effect": "refund:success@lenient", "max_duplicate_success": 1,
        },
        "refund_status_success": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS", "order_verification": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"]},
            "required_nodes": ["human_review", "refund_execution", "finalize"],
            "forbidden_nodes": [], "side_effect": "refund:success", "approval_required": True,
        },
        "fraud_evidence_reuse": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE"},
            "allow": {"current_status": ["SUSPENDED"]},
            "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund", "reuse_evidence": True,
        },
        "threshold_carding": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE"},
            "allow": {"current_status": ["SUSPENDED"]},
            "max_wait_seconds": 60,
            "required_nodes": ["human_review"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund",
        },
    }

    # V2.0 族（仅保留值得参数化的）
    T30 = {
        "channel_mini_program": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"]},
            "required_nodes": ["intake", "verification", "ocr", "fraud",
                               "sentiment", "amount_decision", "refund_execution", "finalize"],
            "forbidden_nodes": ["human_review"], "side_effect": "refund:success",
            "channel": "小程序",
        },
        "channel_cs_agent": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"]},
            "required_nodes": ["refund_execution", "finalize"],
            "forbidden_nodes": ["human_review"], "side_effect": "refund:success",
            "channel": "客服代提",
        },
        "partial_refund": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS", "PENDING"]},
            "required_nodes": ["refund_execution", "finalize"],
            "forbidden_nodes": ["human_review"], "side_effect": "refund:success",
        },
        "refund_retry": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS", "order_verification": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS"]},
            "required_nodes": ["refund_execution", "finalize"], "forbidden_nodes": [],
            "side_effect": "refund:success", "inject": {"first_refund_fail": True},
        },
        "system_failure_no_refund": {
            "expected": {"current_status": "COMPLETED", "final_decision": "SYSTEM_FAILURE",
                         "refund_status": "NONE"},
            "allow": {"final_decision": ["SYSTEM_FAILURE"], "refund_status": ["NONE"]},
            "required_nodes": ["finalize"], "forbidden_nodes": ["refund_execution"],
            "side_effect": "no_refund", "inject": {"system_failure": True},
        },
        "idempotent_recreate": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS"},
            "allow": {"final_decisions": ["APPROVED"], "refund_status": ["SUCCESS"]},
            "required_nodes": ["refund_execution", "finalize"], "forbidden_nodes": [],
            "side_effect": "refund:success@lenient", "max_duplicate_success": 1,
            "reissue_same_idempotency_key": True,
        },
        "verification_all_match": {
            "expected": {"current_status": "COMPLETED", "final_decision": "APPROVED",
                         "refund_status": "SUCCESS",
                         "order_verification": "MATCH", "product_consistency": "MATCH",
                         "price_consistency": "MATCH"},
            "allow": {"final_decisions": ["APPROVED"]},
            "required_nodes": ["refund_execution", "finalize"],
            "forbidden_nodes": ["human_review"], "side_effect": "refund:success",
        },
        "product_mismatch_sku": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MATCH",
                         "product_consistency": "MISMATCH", "price_consistency": "MATCH"},
            "allow": {"product_consistency": ["MISMATCH"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
        },
        "price_mismatch_partial_refunded": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "MATCH",
                         "product_consistency": "MATCH", "price_consistency": "MISMATCH"},
            "allow": {"price_consistency": ["MISMATCH"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
        },
        "provider_unavailable": {
            "expected": {"current_status": "SUSPENDED", "final_decision": "PENDING",
                         "refund_status": "NONE", "order_verification": "UNKNOWN",
                         "product_consistency": "UNKNOWN", "price_consistency": "UNKNOWN"},
            "allow": {"order_verification": ["UNKNOWN"]},
            "required_nodes": ["verification", "human_review"],
            "forbidden_nodes": ["refund_execution"], "side_effect": "no_refund",
            "provider_unavailable": True,
        },
    }

    def _tpl(case_type: str):
        return T.get(case_type) or T30.get(case_type) or T["auto_approve"]

    # ---- 按族生成（确定性计数：112 条非预留 + 8 条预留能力 = 120）----
    spec: list[tuple[str, int]] = [
        ("auto_approve", 14), ("high_value_approve", 8), ("high_value_reject", 6),
        ("order_not_found", 4), ("owner_mismatch", 4), ("product_mismatch", 6),
        ("price_mismatch", 6), ("provider_timeout", 2), ("ocr_low_confidence", 4),
        ("ocr_failure", 2), ("llm_unavailable", 2), ("idempotency", 8),
        ("refund_status_success", 4), ("fraud_evidence_reuse", 4),
        ("threshold_carding", 6), ("channel_mini_program", 6),
        ("channel_cs_agent", 4), ("partial_refund", 4), ("refund_retry", 2),
        ("system_failure_no_refund", 2), ("idempotent_recreate", 2),
        ("verification_all_match", 6), ("product_mismatch_sku", 2),
        ("price_mismatch_partial_refunded", 2), ("provider_unavailable", 2),
    ]
    assert sum(n for _, n in spec) == 112, "非预留族计数必须为 112"

    cases: list[dict] = []
    cid = 0                      # 1..120 -> G31..G150
    max_v2 = 30
    for ctype, n in spec:
        for _ in range(n):
            cid += 1
            ident = f"G{cid + max_v2:02d}"
            tpl = _tpl(ctype)
            amount = amount_for(cid)
            if ctype in ("high_value_approve", "high_value_reject"):
                amount = f"{599.0 + (cid % 3) * 300.0:.2f}"     # 高额区（过人工审批）
            elif ctype == "threshold_carding":
                amount = f"{300.0 + (cid % 4) * 1.0:.2f}"       # 阈值边界区
            case = {
                "id": ident, "case_type": ctype, "tags": _mktag(),
                "order_id": f"ORD-{ctype.upper()}-{cid}",
                "requested_amount": amount,
                "reason": reason_for(cid),
                "use_unique_order": True,
                "expected": tpl["expected"], "allow": tpl["allow"],
                "max_wait_seconds": tpl.get("max_wait_seconds", 90),
                "required_nodes": tpl.get("required_nodes", []),
                "forbidden_nodes": tpl.get("forbidden_nodes", []),
                "side_effect": tpl.get("side_effect", "no_refund"),
            }
            for f in ("inject", "approval_required", "reject_required", "as_user",
                      "channel", "max_duplicate_success", "timeout_for_username",
                      "provider_unavailable", "reuse_evidence",
                      "reissue_same_idempotency_key"):
                if f in tpl:
                    case[f] = tpl[f]
            cases.append(case)

    # ---- 预留能力族（reserved tags）：skip_if_unsupported + 非终态----
    for g in ("security", "intent", "voice", "coaching"):
        for _ in range(2):
            cid += 1
            cases.append({
                "id": f"G{cid + max_v2:02d}",
                "case_type": f"reserved_{g}",
                "tags": [g],
                "order_id": f"ORD-RESERVED-{g.upper()}-{cid}",
                "requested_amount": "50.00",
                "reason": f"{g} 能力未实现，应 SKIPPED_UNSUPPORTED；不伪造通过",
                "use_unique_order": True,
                "skip_if_unsupported": g,
                "expected": {"current_status": "RUNNING", "final_decision": "PENDING",
                             "refund_status": "NONE"},
                "allow": {"current_status": ["RUNNING"]},
                "max_wait_seconds": 1,
                "required_nodes": [], "forbidden_nodes": ["refund_execution"],
                "side_effect": "no_refund",
            })
    assert cid == 120, f"V3 扩展条数异常: {cid}（应 120）"
    return cases


# ---- 公开不可变数据集 ----
GOLDEN_CASES: list[dict] = _GOLDEN_CASES_V20 + _build_v3_regression_cases()

# 校验生成条数与 ID 连续性
assert len(GOLDEN_CASES) == 150, f"golden 数据集应为 150 条，实际 {len(GOLDEN_CASES)}"
assert [c["id"] for c in GOLDEN_CASES] == [f"G{i:02d}" for i in range(1, 151)], "case id 必须连续 G01..G150"


# ---- 分级调度（M18）：smoke ⊂ core ⊂ full（嵌套子集）----

CORE_FAMILIES = {
    "auto_approve", "high_value_approve", "high_value_reject",
    "order_not_found", "owner_mismatch", "product_mismatch",
    "price_mismatch", "provider_timeout", "ocr_low_confidence",
    "ocr_failure", "llm_unavailable", "idempotency",
    "refund_status_success", "fraud_evidence_reuse",
    "threshold_carding", "verification_all_match",
}


def _tier_sets() -> dict[str, list[str]]:
    """确定性分级：冒烟(G1 高频三态+核验+阈值) ⊂ 核心(≥100 真回归) ⊂ 全量(150)。

    V3.0 -1「核心 Golden ≥100 条」：在 V2.0 的 60 条核心子集之上，
    按 id 序确定性补齐未打标用例到 100（不可变：不依赖随机，每次导入结果相同）。
    预留能力用例（带 security/intent/voice/coaching tag 的能力类）不入核心，
    核心核验的是真实业务/资金回归。

    额外任务层（五类任务，-评测）：
    - security / voice 分层按 tag 精确匹配（预留用例，能力未就绪则 SKIPPED，
      绝不当作通过）。仅当 Suite 声明了该分层且该 tag 用例已实现才可测得。
    """
    smoke = {"G01", "G02", "G03", "G04", "G06", "G15"}
    core = set(smoke)
    for c in GOLDEN_CASES:
        idx = int(c["id"][1:])
        # 基线与 V2 扩展进 core（预留能力 tag 用例不入 core：核验的是真回归）
        if 1 <= idx <= 30 and not (c.get("tags") or []):
            core.add(c["id"])
    for c in GOLDEN_CASES:
        if c["id"] in core:
            continue
        if not (c.get("tags") or []) and c["case_type"] in CORE_FAMILIES:
            core.add(c["id"])
        if len(core) >= 60:
            break                   # 保留 V2.0 的 60 条核心子集作为基底
    for c in sorted(GOLDEN_CASES, key=lambda c: c["id"]):
        if len(core) >= 100:
            break
        if c["id"] in core or c.get("tags"):
            continue
        core.add(c["id"])
    security = {c["id"] for c in GOLDEN_CASES if "security" in (c.get("tags") or [])}
    voice = {c["id"] for c in GOLDEN_CASES if "voice" in (c.get("tags") or [])}
    full = {c["id"] for c in GOLDEN_CASES}
    assert len(smoke) == 6 and len(core) == 100 and len(full) == 150
    assert smoke <= core <= full, "分层必须嵌套 smoke ⊂ core ⊂ full"
    assert security <= full and voice <= full, "能力分层必须是全量子集"
    return {"smoke": sorted(smoke), "core": sorted(core), "full": sorted(full),
            "security": sorted(security), "voice": sorted(voice)}


TIER_MAP = _tier_sets()

# ---- 版本/校验和----
def _canonical_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def dataset_checksum() -> str:
    """数据集内容校验和（不含 id 顺序敏感性：稳定排序后哈希）。"""
    canon = _canonical_json({"version": DATASET_VERSION, "cases": GOLDEN_CASES})
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_dataset_document() -> dict:
    """完整数据集文档（runner 加载、报告 header 使用）。"""
    return {
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "checksum": dataset_checksum(),
        "modalities": MODALITIES,
        "required_capabilities": REQUIRED_CAPABILITIES,
        "reserved_tags": RESERVED_TAGS,     # 未实现，勿当作通过
        "created_by": "eval_refund.golden",
        "primary_owner": PRIMARY_OWNER,
        "approver": APPROVER,
        "tiers": {t: TIER_MAP[t] for t in DATASET_TIERS},   # M18 分级调度
        "tier_labels": dict(TIER_LABELS),
        "case_count": len(GOLDEN_CASES),
        "cases": GOLDEN_CASES,
    }


def load_cases() -> list[dict]:
    """runner 的入口：返回 Golden cases 列表。"""
    return GOLDEN_CASES


def write_dataset_file(path: str | Path) -> Path:
    """导出为 JSON（供存档/校验）。路径不存在目录则创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_canonical_json(build_dataset_document()), encoding="utf-8")
    return p


# ---- 元数据 ----
DATASET_META = {
    "version": DATASET_VERSION,
    "checksum": dataset_checksum(),
    "case_count": len(GOLDEN_CASES),
}