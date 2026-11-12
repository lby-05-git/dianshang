# -*- coding: utf-8 -*-
"""商城下单→生成凭证→退货/换货→OCR核验的 100 条隔离回放。

这里只在临时目录构造 EVAL 订单和图片，不写商城 orders.json、不写业务案件表，
也不调用退款适配器。指标全部来自本轮图片生成和 OCR 执行结果。
"""
from __future__ import annotations

import hashlib
import re
import statistics
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.infrastructure.ocr import OcrResult
from app.infrastructure.order_provider import OrderItem
from app.services.ocr_order_match import cross_check_ocr_fields


DATASET_VERSION = "workorder8-shopping-return-exchange-v1.0.0"
DEFAULT_SAMPLE_COUNT = 100
_PRODUCTS = [
    ("EGET-1001", "vivo S50t 元气版", Decimal("2999.00")),
    ("EGET-1002", "iQOO Z11S 手机", Decimal("2399.00")),
    ("EGET-1003", "iQOO TWS 5e 耳机", Decimal("125.00")),
    ("EGET-1004", "vivo Pad 平板", Decimal("1899.00")),
    ("EGET-1005", "vivo WATCH 智能手表", Decimal("799.00")),
    ("EGET-1006", "闪充充电器", Decimal("169.00")),
    ("EGET-1007", "无线运动耳机", Decimal("299.00")),
    ("EGET-1008", "手机保护壳", Decimal("49.00")),
    ("EGET-1009", "蓝牙音箱", Decimal("199.00")),
    ("EGET-1010", "移动电源", Decimal("229.00")),
]


def build_shopping_flow_cases(sample_count: int = DEFAULT_SAMPLE_COUNT) -> list[dict]:
    rows = []
    for index in range(max(1, sample_count)):
        sku, name, price = _PRODUCTS[index % len(_PRODUCTS)]
        number = index + 1
        rows.append({
            "case_id": f"EVAL-SHOP-{number:04d}",
            "account": str(13900000000 + number),
            "order_id": f"EVAL-ORD-{number:04d}",
            "sku": sku,
            "product_name": name,
            "amount": price,
            "aftersale_type": "return" if number % 2 else "exchange",
            "reason": "商品不合适，申请退货" if number % 2 else "商品型号不合适，申请换货",
        })
    return rows


def dataset_checksum(rows: list[dict]) -> str:
    payload = "\n".join(
        f"{r['case_id']}|{r['account']}|{r['order_id']}|{r['sku']}|"
        f"{r['product_name']}|{r['amount']}|{r['aftersale_type']}"
        for r in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
             else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
             else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def _render_voucher(case: dict, target: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 570), "#fffef9")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([2, 2, 997, 567], radius=14, outline="#d7d3c9", width=2)
    draw.rounded_rectangle([3, 3, 996, 88], radius=13, fill="#ce2d2d")
    draw.rectangle([3, 55, 996, 88], fill="#ce2d2d")
    draw.text((30, 20), "eget 速运", font=_font(31, bold=True), fill="#ffffff")
    draw.text((224, 23), "EGET EXPRESS", font=_font(25, bold=True), fill="#ffffff")
    draw.text((775, 25), "电子订单凭证", font=_font(22), fill="#ffffff")
    draw.text((34, 116), f"物流单号：SF{case['case_id'][-4:]}20260907", font=_font(23), fill="#111111")
    draw.text((34, 162), f"订单编号：{case['order_id']}", font=_font(25, bold=True), fill="#111111")
    draw.text((34, 208), f"下单时间：2026-09-07 10:{int(case['case_id'][-4:]) % 60:02d}:00", font=_font(22), fill="#111111")
    draw.line([28, 252, 972, 252], fill="#d4d4d4", width=2)
    draw.text((34, 278), f"收件账号：{case['account']}", font=_font(22), fill="#111111")
    draw.text((34, 324), f"商品SKU：{case['sku']}", font=_font(24, bold=True), fill="#111111")
    draw.text((34, 370), f"商品名称：{case['product_name']}", font=_font(24, bold=True), fill="#111111")
    draw.text((34, 416), f"订单金额：¥{case['amount']:.2f}", font=_font(27, bold=True), fill="#111111")
    draw.text((34, 476), f"售后场景：{'退货退款' if case['aftersale_type'] == 'return' else '换货'}",
              font=_font(22), fill="#333333")
    draw.text((570, 474), "EVAL 隔离回放 · 不执行真实退款", font=_font(20), fill="#8a2d2d")
    image.save(target, format="PNG", optimize=True)


def _latency(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"avg": None, "p95": None, "max": None}
    p95_index = min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))
    return {
        "avg": round(statistics.fmean(ordered), 2),
        "p95": round(ordered[p95_index], 2),
        "max": round(max(ordered), 2),
    }


def evaluate_shopping_return_exchange_flows(
    *,
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    recognizer: Callable[[dict, str], OcrResult] | None = None,
) -> dict:
    """执行商城链路回放；recognizer 仅供单测注入，生产默认调用真实 OCR Provider。"""
    cases = build_shopping_flow_cases(sample_count)
    using_real_ocr = recognizer is None
    if recognizer is None:
        try:
            from app.infrastructure.ocr import get_ocr_provider

            provider = get_ocr_provider()
            recognizer = lambda _case, path: provider.ocr(path)
        except Exception as exc:  # OCR 初始化失败时如实返回未测，不伪造得分
            return {
                "status": "UNAVAILABLE", "measured": False,
                "measurement": "real_ocr_wall_clock", "sample_count": len(cases),
                "attempted": 0, "completed": 0, "completion_rate": None,
                "order_accuracy": None, "product_accuracy": None, "amount_accuracy": None,
                "aftersale_distribution": {"return": len([c for c in cases if c['aftersale_type'] == 'return']),
                                             "exchange": len([c for c in cases if c['aftersale_type'] == 'exchange'])},
                "isolation": {"business_writes": 0, "real_refunds": 0, "mode": "EVAL_IN_MEMORY_ONLY"},
                "error": f"OCR provider 初始化失败: {str(exc)[:180]}",
                "dataset_version": DATASET_VERSION, "checksum": dataset_checksum(cases),
            }

    field_hits = {"order": 0, "product": 0, "amount": 0}
    completed = 0
    attempted = 0
    latencies = []
    failures = []
    started_all = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="rfs-eval-shopping-") as temp_dir:
        for case in cases:
            target = Path(temp_dir) / f"{case['order_id']}.png"
            try:
                _render_voucher(case, target)
                started = time.perf_counter()
                result = recognizer(case, str(target))
                latencies.append((time.perf_counter() - started) * 1000)
                attempted += 1
                fields = dict(result.fields or {})
                order_ok = re.sub(r"\s+", "", str(fields.get("order_id") or "")).upper() == case["order_id"]
                sku_ok = str(fields.get("product_sku") or "").upper() == case["sku"].upper()
                name_ok = case["product_name"].replace(" ", "") in str(fields.get("product_name") or "").replace(" ", "")
                product_ok = sku_ok or name_ok
                try:
                    amount_ok = abs(Decimal(str(fields.get("amount"))) - case["amount"]) <= Decimal("0.01")
                except Exception:
                    amount_ok = False
                field_hits["order"] += int(order_ok)
                field_hits["product"] += int(product_ok)
                field_hits["amount"] += int(amount_ok)
                cross = cross_check_ocr_fields(
                    expected_order_id=case["order_id"], expected_amount=case["amount"],
                    trusted_items=[OrderItem(sku=case["sku"], name=case["product_name"], quantity=1)],
                    fields=fields,
                )
                success = order_ok and product_ok and amount_ok and not cross["updates"]
                completed += int(success)
                if not success and len(failures) < 10:
                    failures.append({"case_id": case["case_id"], "fields": fields,
                                     "confidence": round(float(result.confidence or 0), 4)})
            except Exception as exc:
                attempted += 1
                if len(failures) < 10:
                    failures.append({"case_id": case["case_id"], "error": str(exc)[:180]})

    total = len(cases)
    return {
        "status": "COMPLETED", "measured": using_real_ocr,
        "measurement": "real_ocr_wall_clock" if using_real_ocr else "test_double",
        "sample_count": total, "unique_users": len({c["account"] for c in cases}),
        "attempted": attempted, "completed": completed,
        "completion_rate": round(completed / total, 4) if total else None,
        "order_accuracy": round(field_hits["order"] / total, 4) if total else None,
        "product_accuracy": round(field_hits["product"] / total, 4) if total else None,
        "amount_accuracy": round(field_hits["amount"] / total, 4) if total else None,
        "ocr_latency_ms": _latency(latencies),
        "duration_ms": round((time.perf_counter() - started_all) * 1000, 2),
        "aftersale_distribution": {
            "return": sum(c["aftersale_type"] == "return" for c in cases),
            "exchange": sum(c["aftersale_type"] == "exchange" for c in cases),
        },
        "failure_examples": failures,
        "isolation": {"business_writes": 0, "real_refunds": 0, "mode": "EVAL_IN_MEMORY_ONLY"},
        "dataset_version": DATASET_VERSION, "checksum": dataset_checksum(cases),
    }
