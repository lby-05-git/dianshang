# -*- coding: utf-8 -*-
from decimal import Decimal

from app.infrastructure.ocr import OcrResult, RapidOcrProvider
from app.infrastructure.order_provider import MISMATCH, OrderItem
from app.services.ocr_order_match import cross_check_ocr_fields
from app.services.shopping_flow_eval import (
    build_shopping_flow_cases,
    evaluate_shopping_return_exchange_flows,
)


def test_builds_100_isolated_balanced_return_exchange_users():
    rows = build_shopping_flow_cases()
    assert len(rows) == 100
    assert len({row["account"] for row in rows}) == 100
    assert len({row["order_id"] for row in rows}) == 100
    assert sum(row["aftersale_type"] == "return" for row in rows) == 50
    assert sum(row["aftersale_type"] == "exchange" for row in rows) == 50


def test_evaluation_replays_voucher_ocr_contract_without_business_writes():
    def recognize(case, _path):
        return OcrResult(
            text="电子订单凭证",
            fields={
                "order_id": case["order_id"], "product_sku": case["sku"],
                "product_name": case["product_name"], "amount": str(case["amount"]),
            },
            confidence=0.99,
        )

    result = evaluate_shopping_return_exchange_flows(recognizer=recognize)
    assert result["sample_count"] == 100
    assert result["unique_users"] == 100
    assert result["completion_rate"] == 1.0
    assert result["order_accuracy"] == 1.0
    assert result["product_accuracy"] == 1.0
    assert result["amount_accuracy"] == 1.0
    assert result["isolation"] == {
        "business_writes": 0, "real_refunds": 0, "mode": "EVAL_IN_MEMORY_ONLY",
    }
    assert result["measured"] is False


def test_order_voucher_field_parser_prefers_labeled_amount_and_product():
    text = (
        "物流单号：SF1378026845219\n订单编号：ORD-20260907-ABC123\n"
        "商品SKU：5675882\n商品名称：vivo S50t 元气版\n订单金额：¥2999.00"
    )
    fields = RapidOcrProvider._extract_fields(text)
    assert fields["order_id"] == "ORD-20260907-ABC123"
    assert fields["product_sku"] == "5675882"
    assert fields["product_name"] == "vivo S50t 元气版"
    assert fields["amount"] == "2999.00"
    assert fields["waybill_no"] == "SF1378026845219"


def test_ocr_conflict_forces_verification_mismatch():
    result = cross_check_ocr_fields(
        expected_order_id="ORD-OK", expected_amount=Decimal("125.00"),
        trusted_items=[OrderItem(sku="SKU-OK", name="无线耳机", quantity=1)],
        fields={"order_id": "ORD-BAD", "product_sku": "SKU-BAD", "amount": "126.00"},
    )
    assert result["updates"] == {
        "order_verification": MISMATCH,
        "price_verification": MISMATCH,
        "product_verification": MISMATCH,
    }
