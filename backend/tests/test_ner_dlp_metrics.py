# -*- coding: utf-8 -*-
"""V3.0 V3-3 DLP 终指标（+ 召回率/误报率报告）。

覆盖：
- 确定性基准标注语料：文档级准确率 ≥ 99% 达标（无漏报/误报才计完美文档）；
- 报告含 recall / false_positive_rate / per_type，未达标如实列出 gaps；
- 基准语料同时含纯负例（裸数字/座机/地址等不误报 PII）。
"""
from app.services import ner_dlp as nd


def test_benchmark_corpus_has_positives_and_negatives():
    docs = nd.benchmark_docs()
    positive = [d for d in docs if d.expected]
    negative = [d for d in docs if not d.expected]
    assert positive and negative
    types = set()
    for d in positive:
        types |= set(d.expected)
    assert nd.DLP_PHONE in types and nd.DLP_ID_CARD in types
    assert len(types) >= 7          # 8 类实体基本全覆盖


def test_final_metrics_accuracy_target_reached():
    """DLP 识别准确率 ≥99%（文档级完美判定）+ 指标报告形状。"""
    res = nd.final_metrics()
    assert res["total"] >= 40
    assert res["accuracy"] >= nd.ACCURACY_TARGET
    assert res["meets_target"] is True
    assert res["gaps"] == []        # 未达标会如实列出，绝不隐去
    assert 0 <= res["recall"] <= 1
    assert 0 <= res["false_positive_rate"] <= 1
    assert res["per_type"]           # 逐类型指标


def test_final_metrics_reports_failure_honestly():
    """未达标语料必须如实报 accuracy 且 meets_target=False（不假报通过）。"""
    docs = [
        nd.LabeledDoc("完全无关文本", set()),
        nd.LabeledDoc("手机 13812345678", {nd.DLP_ORDER_NO}),   # 误判：漏号+多判
    ]
    res = nd.final_metrics(docs)
    assert res["accuracy"] < 1.0
    assert res["meets_target"] is False
    assert res["gaps"], "未达标必须列出缺口"
    assert res["gaps"][0]["missed"] or res["gaps"][0]["fp"]


def test_mask_never_leaks_plaintext_in_benchmark():
    """基准语料脱敏结果绝不残留非 NONE 实体明文（DLP 红线）。

    ORDER_NO 是唯一允许 NONE 动作的类型（业务标识必须保留到上下文），其余实体
    （手机/身份证/银行卡/密钥/邮箱/姓名/伪身份）掩码后明文必须消失。
    """
    docs = nd.benchmark_docs()
    leaked = []
    for d in docs:
        masked, entities = nd.mask(d.text)
        for e in entities:
            if e.action == "NONE":
                continue
            if e.raw and e.raw.lower() in masked.lower():
                leaked.append((d.text, e.entity_type, e.raw, masked, e.action))
    assert leaked == []