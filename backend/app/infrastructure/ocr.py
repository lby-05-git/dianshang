# -*- coding: utf-8 -*-
"""OCR 适配器（裁决 D1：PaddleOCR 优先，失败切 RapidOCR；docs/07 手册 置信度路由）。

启动时懒加载：先尝试 PaddleOCR（若已安装），否则 RapidOCR（onnxruntime，已验证本机 0.85s/张）。
实测记录（2026-08-17）：paddleocr 3.7.0 + paddlepaddle 3.3.1 在本机（Windows 11 CPU）
引擎初始化报 `json.exception.parse_error`（paddle_static runner 创建 predictor 失败），
已按 D1 裁决正式切换到 RapidOCR（PP-OCRv4 onnxruntime），见 docs/loop-reports/phase-07。
测试可用 FakeOcrProvider。
"""
import logging
from dataclasses import dataclass, field

from app.core.config import settings

logger = logging.getLogger("infra.ocr")


@dataclass
class OcrResult:
    text: str
    fields: dict = field(default_factory=dict)   # 结构化字段（金额/订单号/日期，MVP 从文本中提取）
    confidence: float = 0.0


class RapidOcrProvider:
    """RapidOCR（onnxruntime 加载 PP-OCRv4 模型）。"""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def ocr(self, image_path: str) -> OcrResult:
        result, _elapse = self._engine(image_path)
        if not result:
            return OcrResult(text="", confidence=0.0)
        lines = []
        confs = []
        for box, text, conf in result:
            lines.append(text)
            confs.append(float(conf))
        text = "\n".join(lines)
        confidence = sum(confs) / len(confs) if confs else 0.0
        return OcrResult(text=text, fields=self._extract_fields(text), confidence=confidence)

    @staticmethod
    def _extract_fields(text: str) -> dict:
        """从电子订单/运单中提取订单号、商品、金额、日期和物流单号。"""
        import re

        fields = {}
        compact = str(text or "").replace(" ", "")
        m = re.search(
            r"(?:订单金额|实付金额|申请金额|金额)[：:]?[¥￥Y]?([0-9]{1,8}(?:\.[0-9]{1,2})?)",
            compact, re.IGNORECASE,
        )
        if m:
            fields["amount"] = m.group(1)
        m = re.search(
            r"(?:order|订单)(?:编号|号码|号|#)?[：:]?([A-Z0-9\-]{4,64})",
            compact, re.IGNORECASE,
        )
        if m:
            fields["order_id"] = m.group(1).upper()
        m = re.search(r"(?:商品SKU|SKU)[：:]?([A-Z0-9\-]{2,40})", compact, re.IGNORECASE)
        if m:
            fields["product_sku"] = m.group(1)
        m = re.search(r"商品名称[：:]?([^\n\r]{2,160})", str(text or ""), re.IGNORECASE)
        if m:
            fields["product_name"] = m.group(1).strip()
        m = re.search(r"(?:物流单号|运单号)[：:]?([A-Z0-9\-]{6,40})", compact, re.IGNORECASE)
        if m:
            fields["waybill_no"] = m.group(1).upper()
        m = re.search(r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})", compact)
        if m:
            fields["date"] = m.group(1)
        return fields


class FakeOcrProvider:
    """测试桩：固定返回，置信度可注入。"""

    def __init__(self, text: str = "破损商品，申请退款", confidence: float = 0.95):
        self._text = text
        self._confidence = confidence

    def ocr(self, image_path: str) -> OcrResult:
        return OcrResult(text=self._text, fields={}, confidence=self._confidence)


_provider = None


def get_ocr_provider():
    """全局单例：PaddleOCR 优先，失败 RapidOCR。"""
    global _provider
    if _provider is not None:
        return _provider
    try:
        from paddleocr import PaddleOCR  # noqa: F401

        _provider = _PaddleProvider()
        logger.info("OCR provider: PaddleOCR")
    except Exception as exc:  # noqa: BLE001
        logger.info("PaddleOCR unavailable (%s), use RapidOCR", exc)
        _provider = RapidOcrProvider()
        logger.info("OCR provider: RapidOCR")
    return _provider


class _PaddleProvider:
    """PaddleOCR 官方引擎封装（懒加载，模型首次调用时下载）。"""

    def __init__(self):
        from paddleocr import PaddleOCR

        self._engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    def ocr(self, image_path: str) -> OcrResult:
        result = self._engine.ocr(image_path, cls=True)
        lines, confs = [], []
        for page in result or []:
            for item in page or []:
                if len(item) < 2:
                    continue
                box, (text, conf) = item, item[1]
                lines.append(text)
                confs.append(float(conf))
        text = "\n".join(lines)
        confidence = sum(confs) / len(confs) if confs else 0.0
        return OcrResult(text=text, fields=RapidOcrProvider._extract_fields(text),
                         confidence=confidence)


def confidence_route(confidence: float, field_confidence: float | None = None) -> str:
    """docs/07 手册 三级路由：绿(自动) / 黄(人工) / 红(强制人工)。

    返回 "AUTO" / "HUMAN_REVIEW"（MVP 简化：黄区并入人工，不参与自动决策）。
    """
    threshold = settings.OCR_CONFIDENCE_THRESHOLD
    if confidence >= threshold:
        return "AUTO"
    return "HUMAN_REVIEW"
