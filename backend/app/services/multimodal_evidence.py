# -*- coding: utf-8 -*-
"""传统 OCR 与视觉大模型结果融合、图文风险和舆情安全上下文。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.infrastructure.ocr import OcrResult
from app.infrastructure.vision import VisionAnalysisResult


_INJECTION_PATTERNS = (
    re.compile(r"忽略.{0,12}(?:之前|以上|所有|系统).{0,12}(?:指令|规则|提示)", re.I),
    re.compile(r"(?:system\s*prompt|developer\s*message|系统提示词|开发者指令)", re.I),
    re.compile(r"(?:自动|立即|直接).{0,8}(?:批准|通过|退款|拒绝).{0,8}(?:无需|跳过|绕过)?", re.I),
    re.compile(r"(?:扮演|你现在是|改变角色).{0,30}(?:管理员|系统|审核员|主管)", re.I),
)


def detect_image_prompt_injection(text: str | None) -> bool:
    value = str(text or "")[:5000]
    return any(pattern.search(value) for pattern in _INJECTION_PATTERNS)


def _key(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", str(value or "")).upper()


def _same(field: str, left: object, right: object) -> bool:
    if field == "amount":
        try:
            return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.01")
        except (InvalidOperation, ValueError):
            return _key(left) == _key(right)
    if field == "product_name":
        a, b = _key(left), _key(right)
        return bool(a and b and (a in b or b in a))
    return _key(left) == _key(right)


def _empty_alignment() -> dict:
    return {
        "verdict": "INSUFFICIENT",
        "reason": "视觉模型未返回可用的图文一致性结论",
        "evidence_relevant": None,
    }


def _is_structured_order_voucher(text: str | None, fields: dict | None,
                                 file_name: str | None = None) -> bool:
    """用确定性OCR事实识别商城订单凭证，不依赖视觉模型枚举是否规范。"""
    value = str(text or "")
    name = str(file_name or "")
    data = fields or {}
    has_identity = bool(data.get("order_id") and data.get("amount"))
    has_voucher_marker = (
        "电子订单凭证" in value
        or "EGETEXPRESS" in value.replace(" ", "").upper()
        or "订单凭证" in name
    )
    return has_identity and has_voucher_marker


def fuse_multimodal_evidence(
    *,
    ocr_result: OcrResult,
    ocr_status: str,
    vision_result: VisionAnalysisResult | None,
    vision_error: str | None,
    min_vision_confidence: float,
) -> dict:
    """OCR 优先保留精确字段，VL 补充缺失字段；冲突只标记，不静默覆盖。"""
    ocr_fields = dict(ocr_result.fields or {})
    merged_fields = dict(ocr_fields)
    field_sources = {key: "ocr" for key, value in ocr_fields.items() if value not in (None, "")}
    field_conflicts: list[dict] = []
    vision_fields: dict = {}
    vision_payload: dict
    visual_facts: list[str] = []
    damage = {
        "detected": None,
        "category": "UNKNOWN",
        "type": None,
        "severity": "unknown",
    }
    claim_alignment = _empty_alignment()
    suspicious_signals: list[str] = []
    injection = detect_image_prompt_injection(ocr_result.text)
    structured_voucher = _is_structured_order_voucher(ocr_result.text, ocr_fields)

    if vision_result is not None:
        vision_fields = vision_result.visible_text.model_dump(exclude_none=True)
        if "tracking_no" in vision_fields:
            vision_fields.setdefault("waybill_no", vision_fields["tracking_no"])
        reliable_vision = vision_result.confidence >= min_vision_confidence
        for field, value in vision_fields.items():
            if value in (None, ""):
                continue
            if field in merged_fields and merged_fields[field] not in (None, ""):
                if reliable_vision and not _same(field, merged_fields[field], value):
                    field_conflicts.append({
                        "field": field,
                        "ocr_value": str(merged_fields[field]),
                        "vision_value": str(value),
                    })
                elif reliable_vision:
                    field_sources[field] = "ocr+vision"
            elif reliable_vision:
                merged_fields[field] = value
                field_sources[field] = "vision"
        visual_facts = list(vision_result.visual_facts)
        damage = vision_result.damage.model_dump()
        claim_alignment = vision_result.claim_alignment.model_dump()
        suspicious_signals = list(vision_result.suspicious_signals)
        injection = injection or vision_result.prompt_injection_detected
        vision_payload = {
            "status": "SUCCESS",
            **vision_result.model_dump(),
        }
    else:
        vision_payload = {"status": vision_error or "FAILED"}

    vision_reliable = (
        vision_result is not None
        and vision_result.confidence >= min_vision_confidence
    )
    vision_first_evidence = (
        vision_reliable
        and vision_result.evidence_type in {"damage_photo", "product_photo"}
    )

    if injection:
        evidence_status = "UNSAFE"
    elif field_conflicts:
        evidence_status = "UNRELIABLE"
    elif vision_first_evidence:
        # 实物/破损照片通常本来就没有可 OCR 的文字。视觉结果可靠时，不应因为
        # “OCR 无文本”把整张证据判失败。
        evidence_status = "SUCCESS"
    elif ocr_status == "FAILED":
        # 单据/未知类型仍依赖 OCR 的精确字段；VL 描述不能替代订单号和金额核验。
        evidence_status = "FAILED"
    elif ocr_status == "UNRELIABLE":
        evidence_status = "UNRELIABLE"
    elif not vision_reliable:
        evidence_status = "DEGRADED"
    else:
        evidence_status = "SUCCESS"

    ocr_conf = max(0.0, min(1.0, float(ocr_result.confidence or 0.0)))
    if vision_result is None:
        overall_confidence = ocr_conf
    else:
        vision_conf = max(0.0, min(1.0, float(vision_result.confidence)))
        overall_confidence = round(ocr_conf * 0.55 + vision_conf * 0.45, 4)

    evidence_type = vision_result.evidence_type if vision_result is not None else "other"
    if structured_voucher:
        evidence_type = "order_receipt"

    return {
        "version": "multimodal-evidence-v1",
        "evidence_type": evidence_type,
        "structured_voucher": structured_voucher,
        "evidence_status": evidence_status,
        # 兼容既有 CaseDetail/OCR UI 与 cross_check_ocr_fields。
        "text": ocr_result.text[:2000],
        "fields": merged_fields,
        "confidence": ocr_conf,
        "ocr": {
            "status": ocr_status,
            "text": ocr_result.text[:2000],
            "fields": ocr_fields,
            "confidence": ocr_conf,
        },
        "vision": vision_payload,
        "field_sources": field_sources,
        "field_conflicts": field_conflicts,
        "visual_facts": visual_facts,
        "damage": damage,
        "claim_alignment": claim_alignment,
        "prompt_injection_detected": injection,
        "suspicious_signals": suspicious_signals,
        "overall_confidence": overall_confidence,
    }


def _dimension(verdict: str, reasons: list[str]) -> dict:
    return {"verdict": verdict, "reasons": list(dict.fromkeys(reasons))}


def _application_mentions(text: str, words: tuple[str, ...]) -> bool:
    value = str(text or "").lower()
    return any(word.lower() in value for word in words)


def aggregate_evidence_consistency(*, application: dict, evidences: list[dict]) -> dict:
    """汇总整份售后申请与全部附件，保留逐张结论并生成跨附件裁决。

    该层只做证据判断，不执行退款。证据不足与明确矛盾严格分离：前者转人工
    补件，后者才允许进入风险加分。
    """
    app = dict(application or {})
    rows: list[dict] = []
    summaries: list[dict] = []
    for item in evidences or []:
        summary = dict(item.get("summary") or {})
        summaries.append(summary)
        evidence_type = (
            "order_receipt"
            if summary.get("structured_voucher") or _is_structured_order_voucher(
                summary.get("text"), summary.get("fields"), item.get("file_name")
            )
            else summary.get("evidence_type")
                 or (summary.get("vision") or {}).get("evidence_type")
                 or "other"
        )
        rows.append({
            "evidence_id": str(item.get("evidence_id") or ""),
            "file_name": str(item.get("file_name") or "附件"),
            "evidence_type": evidence_type,
            "structured_voucher": evidence_type == "order_receipt",
            "evidence_status": summary.get("evidence_status") or "FAILED",
            "fields": summary.get("fields") or {},
            "visual_facts": summary.get("visual_facts") or [],
            "damage": summary.get("damage") or {},
            "claim_alignment": summary.get("claim_alignment") or _empty_alignment(),
            "overall_confidence": summary.get("overall_confidence", 0.0),
            "prompt_injection_detected": bool(summary.get("prompt_injection_detected")),
            "field_conflicts": summary.get("field_conflicts") or [],
        })

    expected_order = app.get("order_id")
    expected_amount = app.get("requested_amount")
    expected_skus = {_key(v) for v in (app.get("product_skus") or []) if _key(v)}

    order_values = [r["fields"].get("order_id") for r in rows if r["fields"].get("order_id")]
    amount_values = [r["fields"].get("amount") for r in rows if r["fields"].get("amount")]
    sku_values = [r["fields"].get("product_sku") for r in rows if r["fields"].get("product_sku")]

    if expected_order and any(not _same("order_id", expected_order, value) for value in order_values):
        order_dim = _dimension("MISMATCH", ["附件订单号与申请订单不一致"])
    elif expected_order and any(_same("order_id", expected_order, value) for value in order_values):
        order_dim = _dimension("MATCH", ["至少一份附件订单号与申请订单一致"])
    else:
        order_dim = _dimension("UNKNOWN", ["附件中没有可核验的订单号"])

    normalized_skus = {_key(v) for v in sku_values if _key(v)}
    if expected_skus and normalized_skus and not normalized_skus.issubset(expected_skus):
        product_dim = _dimension("MISMATCH", ["附件商品 SKU 与申请订单商品不一致"])
    elif expected_skus and normalized_skus.intersection(expected_skus):
        product_dim = _dimension("MATCH", ["附件商品 SKU 与申请商品一致"])
    else:
        # 商品照片通常没有可见 SKU；由图文语义结论补充，但不凭空判 MATCH。
        relevant_product = any(
            r["evidence_type"] in {"product_photo", "damage_photo"}
            and (r["claim_alignment"] or {}).get("verdict") == "SUPPORTED"
            for r in rows
        )
        product_dim = _dimension(
            "MATCH" if relevant_product else "UNKNOWN",
            ["实物附件与申请商品语义一致"] if relevant_product else ["缺少可核验的商品标识或实物证据"],
        )

    if expected_amount is not None and any(not _same("amount", expected_amount, value) for value in amount_values):
        price_dim = _dimension("MISMATCH", ["附件金额与申请退款金额不一致"])
    elif expected_amount is not None and any(_same("amount", expected_amount, value) for value in amount_values):
        price_dim = _dimension("MATCH", ["附件金额与申请退款金额一致"])
    else:
        price_dim = _dimension("UNKNOWN", ["附件中没有可核验的金额"])

    claim_text = " ".join([
        str(app.get("refund_reason") or ""),
        " ".join(str(v) for v in (app.get("reason_codes") or [])),
    ])
    damage_claim = _application_mentions(
        claim_text, ("破损", "破裂", "碎裂", "裂纹", "碎屏", "划痕", "进水", "故障", "损坏"),
    )
    logistics_claim = _application_mentions(
        claim_text, ("物流", "快递", "运输", "签收", "丢件", "运单"),
    )
    wrong_item_claim = _application_mentions(
        claim_text, ("错发", "发错", "货不对板", "商品不符", "型号不符"),
    )

    contradicted = [r for r in rows if (r["claim_alignment"] or {}).get("verdict") == "CONTRADICTED"]
    supported_problem = [
        r for r in rows
        if r["evidence_type"] in {"damage_photo", "product_photo"}
        and (r["claim_alignment"] or {}).get("verdict") == "SUPPORTED"
        and (not damage_claim or (r["damage"] or {}).get("detected") is True)
    ]
    if contradicted:
        reason_dim = _dimension("MISMATCH", ["至少一份附件与用户填写的问题描述矛盾"])
    elif supported_problem:
        reason_dim = _dimension("MATCH", ["实物附件能够支持用户填写的问题描述"])
    else:
        reason_dim = _dimension("UNKNOWN", ["现有附件不足以证明用户填写的问题"])

    factual_rows = [
        r for r in rows if r["evidence_type"] in {"damage_photo", "product_photo"}
    ]
    factual_skus = {
        _key(r["fields"].get("product_sku")) for r in factual_rows
        if _key(r["fields"].get("product_sku"))
    }
    if len(factual_skus) > 1 or any(
        (r["claim_alignment"] or {}).get("verdict") == "CONTRADICTED" for r in factual_rows
    ):
        cross_dim = _dimension("MISMATCH", ["多张实物附件的商品标识或问题事实互相冲突"])
    elif len(factual_rows) >= 2 and all(
        (r["claim_alignment"] or {}).get("verdict") == "SUPPORTED" for r in factual_rows
    ):
        cross_dim = _dimension("MATCH", ["多张实物附件从不同角度支持同一商品问题"])
    elif len(factual_rows) == 1:
        cross_dim = _dimension("UNKNOWN", ["只有一张实物附件，无法完成多附件交叉验证"])
    else:
        cross_dim = _dimension("UNKNOWN", ["缺少可用于交叉验证的实物附件"])

    present_types = {r["evidence_type"] for r in rows}
    required = {"order_receipt"}
    if damage_claim:
        required.add("damage_photo")
    if logistics_claim:
        required.add("logistics_waybill")
    if wrong_item_claim:
        required.add("product_photo")
    # product_photo 也可以证明实物破损，但订单凭证不能替代破损实拍。
    missing = sorted(t for t in required if not (
        t in present_types or (t == "damage_photo" and "product_photo" in present_types)
    ))

    dimensions = {
        "order": order_dim,
        "product": product_dim,
        "price": price_dim,
        "reason": reason_dim,
        "cross_attachment": cross_dim,
    }
    unsafe = any(r["prompt_injection_detected"] or r["evidence_status"] == "UNSAFE" for r in rows)
    explicit_mismatch = any(d["verdict"] == "MISMATCH" for d in dimensions.values())
    irrelevant = bool(rows) and any(
        (r["claim_alignment"] or {}).get("evidence_relevant") is False for r in rows
    )
    if unsafe:
        verdict, reason = "UNSAFE", "附件中检测到提示词注入或其他不安全内容"
    elif explicit_mismatch:
        verdict, reason = "CONFLICT", "申请字段、附件内容或多附件之间存在明确冲突"
    elif irrelevant:
        verdict, reason = "UNRELATED", "至少一份附件与本次售后申请无关"
    elif (missing or reason_dim["verdict"] == "UNKNOWN"
          or order_dim["verdict"] == "UNKNOWN"
          or price_dim["verdict"] == "UNKNOWN"):
        verdict, reason = "INSUFFICIENT", "附件尚不足以完整证明本次售后申请"
    else:
        verdict, reason = "CONSISTENT", "订单、商品、金额和问题描述在现有附件中相互印证"

    primary = next(
        (s for s in summaries if s.get("evidence_type") in {"damage_photo", "product_photo"}),
        summaries[0] if summaries else {},
    )
    all_fields: dict = {}
    all_facts: list[str] = []
    all_conflicts: list[dict] = []
    for summary in summaries:
        for key, value in (summary.get("fields") or {}).items():
            all_fields.setdefault(key, value)
        all_facts.extend(str(v) for v in (summary.get("visual_facts") or []))
        all_conflicts.extend(summary.get("field_conflicts") or [])
    confidence_values = [float(r["overall_confidence"] or 0.0) for r in rows]
    alignment_verdict = {
        "CONSISTENT": "SUPPORTED", "CONFLICT": "CONTRADICTED",
        "UNRELATED": "CONTRADICTED", "INSUFFICIENT": "INSUFFICIENT",
        "UNSAFE": "INSUFFICIENT",
    }[verdict]
    evidence_status = {
        "CONSISTENT": "SUCCESS", "CONFLICT": "UNRELIABLE",
        "UNRELATED": "UNRELIABLE", "INSUFFICIENT": "UNRELIABLE", "UNSAFE": "UNSAFE",
    }[verdict]
    return {
        "version": "multimodal-evidence-set-v1",
        "evidence_status": evidence_status,
        "evidence_type": "evidence_set",
        "application": app,
        "attachments": rows,
        "consistency": {
            "verdict": verdict,
            "reason": reason,
            "dimensions": dimensions,
            "required_evidence_types": sorted(required),
            "missing_evidence_types": missing,
        },
        # 以下字段保留旧风控、舆情和主管页面契约。
        "fields": all_fields,
        "text": "\n".join(str(s.get("text") or "") for s in summaries)[:4000],
        "visual_facts": list(dict.fromkeys(all_facts))[:12],
        "damage": primary.get("damage") or {},
        "vision": primary.get("vision") or {},
        "field_conflicts": all_conflicts,
        "claim_alignment": {
            "verdict": alignment_verdict,
            "reason": reason,
            "evidence_relevant": False if verdict == "UNRELATED" else (True if verdict == "CONSISTENT" else None),
        },
        "prompt_injection_detected": unsafe,
        "suspicious_signals": list(dict.fromkeys(
            signal for s in summaries for signal in (s.get("suspicious_signals") or [])
        ))[:12],
        "overall_confidence": round(sum(confidence_values) / len(confidence_values), 4)
                              if confidence_values else 0.0,
        "confidence": round(sum(confidence_values) / len(confidence_values), 4)
                      if confidence_values else 0.0,
        "ocr": primary.get("ocr") or {},
        "field_sources": primary.get("field_sources") or {},
    }


def normalize_stored_evidence_item(summary: dict | None, file_name: str | None = None) -> dict | None:
    """为旧版逐张分析补充确定性的结构化订单凭证分类。"""
    if not isinstance(summary, dict):
        return summary
    result = dict(summary)
    if _is_structured_order_voucher(result.get("text"), result.get("fields"), file_name):
        result["evidence_type"] = "order_receipt"
        result["structured_voucher"] = True
        if (result.get("claim_alignment") or {}).get("verdict") == "INSUFFICIENT":
            result["claim_alignment"] = {
                "verdict": "NOT_APPLICABLE",
                "reason": "OCR已确认结构化订单凭证；该附件用于订单、商品和金额核验",
                "evidence_relevant": True,
            }
    return result


def normalize_stored_evidence_set(summary: dict | None) -> dict | None:
    """只读修复历史集合中被旧VL输出误分为 other 的结构化订单凭证。

    原始 Evidence、Agent Trace 和数据库事实不被改写；API 展示与评分使用基于
    已保存OCR字段重算的派生视图，兼容修复前产生的工单。
    """
    if not isinstance(summary, dict) or summary.get("version") != "multimodal-evidence-set-v1":
        return summary
    attachments = summary.get("attachments") or []
    rebuilt: list[dict] = []
    changed = False
    for row in attachments:
        original = dict(row or {})
        file_name = str(original.get("file_name") or "附件")
        item_summary = normalize_stored_evidence_item(original, file_name) or {}
        if (item_summary.get("evidence_type") == "order_receipt"
                and original.get("evidence_type") != "order_receipt"):
            changed = True
        rebuilt.append({
            "evidence_id": item_summary.get("evidence_id"),
            "file_name": file_name,
            "summary": item_summary,
        })
    if not changed:
        return summary

    recalculated = aggregate_evidence_consistency(
        application=summary.get("application") or {}, evidences=rebuilt,
    )
    result = dict(summary)
    for key in ("evidence_status", "attachments", "consistency", "claim_alignment"):
        result[key] = recalculated[key]
    return result


def score_evidence_risk(
    *,
    summary: dict | None,
    order_verification: str | None = None,
    product_verification: str | None = None,
    price_verification: str | None = None,
) -> tuple[int, list[str]]:
    """返回多模态证据风险增量；UNKNOWN/证据不足不按欺诈计分。"""
    value = dict(summary or {})
    rules: list[str] = []
    score = 0

    if value.get("prompt_injection_detected") or value.get("evidence_status") == "UNSAFE":
        score += 40
        rules.append("image_prompt_injection")

    alignment = value.get("claim_alignment") or {}
    if alignment.get("verdict") == "CONTRADICTED":
        score += 30
        rules.append("claim_contradicted")
    if alignment.get("evidence_relevant") is False:
        score += 30
        rules.append("evidence_irrelevant")

    for verdict, points, name in (
        (order_verification, 40, "order_mismatch"),
        (product_verification, 35, "product_mismatch"),
        (price_verification, 25, "price_mismatch"),
    ):
        if verdict == "MISMATCH":
            score += points
            rules.append(name)

    return min(100, score), list(dict.fromkeys(rules))


def build_sentiment_context(refund_reason: str | None, summary: dict | None) -> str:
    """只向舆情模型提供脱敏诉求和安全视觉事实，不传订单字段或图片指令。"""
    value = dict(summary or {})
    # 检出图片 Prompt 注入后，不把任何图片派生文本继续传给舆情 LLM。
    facts = [] if value.get("prompt_injection_detected") else [
        str(item)[:240] for item in (value.get("visual_facts") or [])[:8]
    ]
    damage = value.get("damage") or {}
    parts = [f"用户退款说明：{(refund_reason or '未提供')[:2000]}"]
    if facts:
        parts.append("图片客观事实：" + "；".join(facts))
    if damage.get("detected") is True and not value.get("prompt_injection_detected"):
        parts.append(
            "视觉问题：检测到损坏，分类=%s，类型=%s，严重程度=%s"
            % (
                damage.get("category") or "UNKNOWN",
                damage.get("type") or "未知",
                damage.get("severity") or "unknown",
            )
        )
    raw = "\n".join(parts)
    try:
        from app.services.ner_dlp import mask

        masked, _entities = mask(raw, role="agent", scenario="refund")
        return masked
    except Exception:
        # DLP 服务异常时仍执行最小本地掩码，禁止手机号流向云端舆情模型。
        return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "【手机已掩码】", raw)
