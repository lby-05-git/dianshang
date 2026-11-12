# -*- coding: utf-8 -*-
"""订单/商品/价格核验。

三个核验是确定性规则（不依赖 LLM），输入来自 RefundCase 行 + MockOrderProvider：

  order_verification   订单真实性（存在 + 归属 + 交易状态 + 可退资格）
  product_verification 商品一致性（reason/SKU 与订单商品比对）
  price_verification   价格一致性（申请金额 <= 可退金额，且 >= 0）

结果口径统一为：
  MATCH   核验通过
  MISMATCH核验失败（存在事实冲突）-> 强制挂起，不自动退款
  UNKNOWN 数据源不可用（Provider超时/异常）-> 转人工，不自动退款

被 workflow/graph.py 的 verification_stage_node 调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.infrastructure.order_provider import (
    MATCH,
    MISMATCH,
    OrderProvider,
    OrderRecord,
    UNKNOWN,
    get_order_provider,
    verify_order,
)

logger = logging.getLogger("workflow.verification")


@dataclass
class VerificationOutcome:
    """三个核验 + 结构化原因（供 state / 写库 / 报告）。"""
    order_verification: str = UNKNOWN
    order_reasons: list[str] = field(default_factory=list)
    product_verification: str = UNKNOWN
    product_reasons: list[str] = field(default_factory=list)
    price_verification: str = UNKNOWN
    price_reasons: list[str] = field(default_factory=list)

    def as_state(self) -> dict:
        return {
            "order_verification": self.order_verification,
            "order_verify_reasons": self.order_reasons,
            "product_verification": self.product_verification,
            "product_verify_reasons": self.product_reasons,
            "price_verification": self.price_verification,
            "price_verify_reasons": self.price_reasons,
        }

    def as_snapshot(self) -> dict:
        """挂起前的检查点摘要：核验结果 + 原因。"""
        return {
            "order_verification": self.order_verification,
            "product_verification": self.product_verification,
            "price_verification": self.price_verification,
            "order_reasons": self.order_reasons,
            "product_reasons": self.product_reasons,
            "price_reasons": self.price_reasons,
        }


def verify_product_from_reason(
    order: OrderRecord,
    user_reason: str | None,
) -> tuple[str, list[str]]:
    """商品一致性：用户诉求（refund_reason）与订单商品/SKU/规格比对。

    V1.0 确定性启发式：reason 包含订单任一商品名/SKU 视为 MATCH；
    无 reason 或完全无命中 -> 若订单只有一个商品仍 MATCH（合理默认），否则 UNKNOWN。
    MISMATCH 仅当 reason 明确声称与订单商品不符时触发。
    """
    if not user_reason:
        return MATCH, []
    reasons: list[str] = []
    order_names = {it.name for it in order.items}
    order_skus = {it.sku for it in order.items}
    claimed = user_reason

    # 明确声称非订单商品 -> MISMATCH
    if any(kw in claimed for kw in ("不是这个", "并不是", "与我订单不符")):
        reasons.append("用户声称的商品与订单商品不一致")
        return MISMATCH, reasons

    hit = any(n in claimed for n in order_names) or any(s in claimed for s in order_skus)
    if hit:
        reasons.append("用户商品诉求命中订单商品")
        return MATCH, reasons

    # 未明确命中：单商品订单保守视为一致；多商品且 reason 无关键词 -> UNKNOWN（不妄断）
    if len(order.items) == 1:
        reasons.append("未检测到冲突商品，单商品订单视为一致")
        return MATCH, reasons
    reasons.append("reason 未命中订单任一商品，多商品订单置 UNKNOWN 转人工")
    return UNKNOWN, reasons


def verify_price(
    requested_amount_yuan: float,
    refundable_amount_yuan: float | None,
) -> tuple[str, list[str]]:
    """价格一致性：申请金额 <= 可退金额，且 > 0。
    参考 POCT：申请额大于可退额 -> MISMATCH；可退额未知 -> UNKNOWN。
    """
    reasons: list[str] = []
    if refundable_amount_yuan is None:
        reasons.append("订单可退金额未知，无法核验价格")
        return UNKNOWN, reasons
    if requested_amount_yuan <= 0:
        reasons.append("申请金额非法（<=0）")
        return MISMATCH, reasons
    if requested_amount_yuan > refundable_amount_yuan:
        reasons.append(
            f"申请金额 {requested_amount_yuan} 大于可退金额 {refundable_amount_yuan}")
        return MISMATCH, reasons
    reasons.append(
        f"申请金额 {requested_amount_yuan} 在可退金额 {refundable_amount_yuan} 内")
    return MATCH, reasons


def run_verification(
    *,
    order_id: str,
    requester_username: str,
    requested_amount_yuan: float,
    refund_reason: str | None = None,
    provider: OrderProvider | None = None,
) -> VerificationOutcome:
    """一次跑三个核验，返回结构化结果。Provider 超时/异常会映射到 UNKNOWN 而非伪 pass。"""
    prov = provider or get_order_provider()
    out = VerificationOutcome()

    try:
        # 订单真实性（含存在/归属/状态/可退资格）
        order_res = verify_order(prov, order_id, requester_username)
        out.order_verification = order_res["order_verification"]
        out.order_reasons = order_res["reasons"]
        order: OrderRecord | None = order_res.get("order")
    except Exception as exc:  # noqa: BLE001 —— Provider 异常不可伪过
        logger.warning("order verify threw: %s", exc)
        out.order_verification = UNKNOWN
        out.order_reasons = [f"订单源不可用: {exc}"]
        order = None

    # 商品一致性：仅在订单核验拿到可用订单时才能做（不存在/超时则 UNKNOWN）
    if order is None:
        out.product_verification = UNKNOWN
        out.product_reasons = ["订单不可用，无法核验商品一致性"]
    else:
        try:
            verdict, reasons = verify_product_from_reason(order, refund_reason)
            out.product_verification = verdict
            out.product_reasons = reasons or ["商品一致性核验通过"]
        except Exception as exc:  # noqa: BLE001
            out.product_verification = UNKNOWN
            out.product_reasons = [f"商品核验异常: {exc}"]

    # 价格一致性
    try:
        refundable = order.refundable_amount_yuan if order is not None else None
        verdict, reasons = verify_price(requested_amount_yuan, refundable)
        out.price_verification = verdict
        out.price_reasons = reasons
    except Exception as exc:  # noqa: BLE001
        out.price_verification = UNKNOWN
        out.price_reasons = [f"价格核验异常: {exc}"]

    return out