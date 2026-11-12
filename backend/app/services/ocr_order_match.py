# -*- coding: utf-8 -*-
"""OCR 结构化字段与可信订单事实交叉核验（纯函数，无业务副作用）。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable

from app.infrastructure.order_provider import MISMATCH, OrderItem


def _key(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", str(value or "")).upper()


def cross_check_ocr_fields(
    *,
    expected_order_id: str,
    expected_amount: Decimal,
    trusted_items: Iterable[OrderItem],
    fields: dict | None,
) -> dict:
    """只在 OCR 给出明确冲突事实时返回 MISMATCH；字段缺失交给置信度路由处理。"""
    fields = dict(fields or {})
    updates: dict[str, str] = {}
    reasons: dict[str, list[str]] = {"order": [], "product": [], "price": []}

    ocr_order = fields.get("order_id")
    if ocr_order and _key(ocr_order) != _key(expected_order_id):
        updates["order_verification"] = MISMATCH
        reasons["order"].append(
            f"OCR订单号 {ocr_order} 与申请订单号 {expected_order_id} 不一致")

    ocr_amount = fields.get("amount")
    if ocr_amount:
        try:
            parsed = Decimal(str(ocr_amount)).quantize(Decimal("0.01"))
            expected = Decimal(expected_amount).quantize(Decimal("0.01"))
            if abs(parsed - expected) > Decimal("0.01"):
                updates["price_verification"] = MISMATCH
                reasons["price"].append(
                    f"OCR订单金额 {parsed} 与申请金额 {expected} 不一致")
        except (InvalidOperation, ValueError):
            updates["price_verification"] = MISMATCH
            reasons["price"].append(f"OCR订单金额无法解析: {ocr_amount}")

    items = list(trusted_items or [])
    trusted_skus = {_key(item.sku) for item in items if item.sku}
    trusted_names = [_key(item.name) for item in items if item.name]
    ocr_sku = fields.get("product_sku")
    ocr_name = fields.get("product_name")
    sku_mismatch = bool(ocr_sku and trusted_skus and _key(ocr_sku) not in trusted_skus)
    name_key = _key(ocr_name)
    name_match = bool(name_key and any(
        name in name_key or name_key in name for name in trusted_names
    ))
    name_mismatch = bool(ocr_name and trusted_names and not name_match)
    if sku_mismatch or (not ocr_sku and name_mismatch):
        updates["product_verification"] = MISMATCH
        if sku_mismatch:
            reasons["product"].append(f"OCR商品SKU {ocr_sku} 不在可信订单商品中")
        if name_mismatch:
            reasons["product"].append(f"OCR商品名称 {ocr_name} 与可信订单商品不一致")

    return {"updates": updates, "reasons": reasons}
