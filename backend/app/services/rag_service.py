# -*- coding: utf-8 -*-
"""第一阶段只读知识客服：中文稀疏向量检索、引用与不确定兜底。"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Iterable


KB_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "customer_service.json"
UNCERTAIN_ANSWER = "不确定。我暂时无法从知识库中找到足够可靠的答案，请联系人工客服进一步确认。"
READ_ONLY_ANSWER = (
    "当前是第一阶段知识客服，只能回答商品、物流、售后和退款规则，"
    "不能修改订单、取消订单、变更地址、提交或执行退款。请在商城对应页面操作或联系人工客服。"
)

_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9.+_-]*", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_MUTATION_REQUEST = re.compile(
    r"(?:帮我|替我|立即|马上|直接|现在).{0,8}(?:退款|退钱|取消订单|删除订单|修改订单|改地址|改价|下单|付款)"
    r"|(?:执行|发起|批准|操作).{0,6}(?:退款|退货|换货|订单)",
    re.IGNORECASE,
)
_QUERY_EXPANSIONS = {
    "快递": "物流 配送 发货 到货",
    "退钱": "退款 售后 到账",
    "没到": "物流 延迟 到货",
    "售后": "退款 退货 换货",
    "多少钱": "商品 价格",
    "人工": "主管 审核 客服",
}


@lru_cache(maxsize=1)
def load_knowledge() -> tuple[dict, ...]:
    raw = json.loads(KB_PATH.read_text(encoding="utf-8"))
    return tuple(dict(item) for item in raw)


def _terms(text: str) -> Counter:
    normalized = str(text or "").lower().strip()
    for source, expanded in _QUERY_EXPANSIONS.items():
        if source in normalized:
            normalized += " " + expanded
    tokens: list[str] = _ASCII_WORD.findall(normalized)
    for run in _CJK_RUN.findall(normalized):
        tokens.extend(run)
        tokens.extend(run[i:i + 2] for i in range(max(0, len(run) - 1)))
    return Counter(tokens)


def _document_text(doc: dict) -> str:
    keywords = " ".join(str(item) for item in (doc.get("keywords") or []))
    return f"{doc.get('title', '')} {doc.get('title', '')} {doc.get('section', '')} {keywords} {doc.get('answer', '')}"


def _cosine_scores(question: str, documents: list[dict]) -> list[float]:
    query_tf = _terms(question)
    doc_tfs = [_terms(_document_text(doc)) for doc in documents]
    if not query_tf or not doc_tfs:
        return [0.0] * len(documents)

    df = Counter()
    for tf in doc_tfs:
        df.update(tf.keys())
    total = len(documents)

    def weighted(tf: Counter) -> dict[str, float]:
        return {
            term: (1.0 + math.log(count)) * (math.log((total + 1) / (df.get(term, 0) + 1)) + 1.0)
            for term, count in tf.items()
        }

    query = weighted(query_tf)
    query_norm = math.sqrt(sum(value * value for value in query.values())) or 1.0
    scores: list[float] = []
    for tf in doc_tfs:
        vector = weighted(tf)
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        dot = sum(value * vector.get(term, 0.0) for term, value in query.items())
        scores.append(dot / (query_norm * norm))
    return scores


def read_only_violation(question: str) -> bool:
    return bool(_MUTATION_REQUEST.search(question or ""))


def _clean_catalog(documents: Iterable[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for index, item in enumerate(documents or []):
        if len(cleaned) >= 12:
            break
        title = str(item.get("title") or "").strip()[:160]
        answer = str(item.get("answer") or "").strip()[:500]
        if not title or not answer:
            continue
        cleaned.append({
            "id": str(item.get("id") or f"catalog-{index}")[:80],
            "section": "商品",
            "title": title,
            "keywords": [title],
            "answer": answer,
        })
    return cleaned


def answer_question(question: str, *, catalog_documents: Iterable[dict] | None = None) -> dict:
    """基于知识证据生成可控回答；不执行 LLM 或任何业务工具。"""
    if read_only_violation(question):
        return {
            "answer": READ_ONLY_ANSWER,
            "uncertain": False,
            "confidence": 1.0,
            "sources": [{
                "id": "assistant-boundary", "section": "服务边界",
                "title": "知识客服能力边界", "snippet": READ_ONLY_ANSWER, "score": 1.0,
            }],
            "boundary_blocked": True,
        }

    documents = list(load_knowledge()) + _clean_catalog(catalog_documents)
    scores = _cosine_scores(question, documents)
    ranked = sorted(zip(documents, scores), key=lambda item: item[1], reverse=True)
    top_score = ranked[0][1] if ranked else 0.0
    if top_score < 0.12:
        return {
            "answer": UNCERTAIN_ANSWER, "uncertain": True,
            "confidence": round(top_score, 4), "sources": [], "boundary_blocked": False,
        }

    selected = [item for item in ranked[:4] if item[1] >= max(0.12, top_score * 0.58)][:3]
    answers: list[str] = []
    sources: list[dict] = []
    for index, (doc, score) in enumerate(selected, start=1):
        answer = str(doc["answer"]).strip()
        if answer not in answers:
            answers.append(answer)
            sources.append({
                "id": doc["id"], "section": doc["section"], "title": doc["title"],
                "snippet": answer[:180], "score": round(score, 4),
            })
    grounded = "\n\n".join(f"{text} [{index}]" for index, text in enumerate(answers, start=1))
    return {
        "answer": grounded,
        "uncertain": False,
        "confidence": round(top_score, 4),
        "sources": sources,
        "boundary_blocked": False,
    }
