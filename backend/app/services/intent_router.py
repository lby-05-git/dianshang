# -*- coding: utf-8 -*-
"""两级意图路由（基线 V1.0 最小能力）。

规则层（正则/Trie）命中 6 意图 -> 直接定意图；未命中 -> LLM 层（可选注入），
输出一次 JSON 修复 + 有限重试（2 次）；LLM 不可用 / 输出不合法 -> Fail-Safe 转 HUMAN。

红线：
- 只落原文 SHA-256，绝不落敏感明文（见 intent_results.raw_sha256）；
- LLM 层任何失败都不抛错扩散，只会把 final_intent 兜成 HUMAN（人工接管）；
- 本模块是纯服务层：不直接改退款，不改业务状态机。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

from app.domain.models import (
    INTENT_APPEAL,
    INTENT_COMPLAINT_ESCALATE,
    INTENT_EVIDENCE,
    INTENT_HUMAN,
    INTENT_QUERY,
    INTENT_REFUND,
    INTENT_UNKNOWN,
    IntentFeedbackSample,
    IntentResult,
    new_uuid,
)

logger = logging.getLogger("services.intent_router")

# 合法意图全集（仅 6 个业务意图；UNKNOWN 为规则层未命中时内部占位，不对外输出）
VALID_INTENTS = frozenset({
    INTENT_REFUND, INTENT_QUERY, INTENT_EVIDENCE,
    INTENT_APPEAL, INTENT_HUMAN, INTENT_COMPLAINT_ESCALATE,
})

# 规则层：每个意图一组关键词/正则（词面尽量宽松，避免误杀普通客诉文本）
_RULES: dict[str, list[str]] = {
    INTENT_REFUND: [
        r"退款", r"退钱", r"退款申请", r"要退款", r"请退款", r"退运费",
        r"退货退款", r"钱退", r"退掉", r"申请退款",
    ],
    INTENT_QUERY: [
        r"物流", r"到哪了", r"快递", r"查单", r"订单状态", r"什么时候到",
        r"没收到", r"发货了吗", r"寄到哪", r"进度",
    ],
    INTENT_EVIDENCE: [
        r"凭证", r"票据", r"截图", r"开箱视频", r"发票", r"证明", r"证据",
        r"聊天记录", r"物流单号", r"快递单号",
    ],
    INTENT_APPEAL: [
        r"申诉", r"复议", r"不服", r"再次申请", r"抗议", r"投诉到底", r"不给退款就投诉",
    ],
    INTENT_HUMAN: [
        r"人工", r"转人工", r"找客服", r"真人", r"人工客服", r"接人工", r"客服帮",
    ],
    INTENT_COMPLAINT_ESCALATE: [
        r"投诉", r"举报", r"12315", r"市场监管局", r"曝光", r"媒体", r"上头条",
        r"法律", r"起诉", r"律师",
    ],
}

# LLM 层输出契约（JSON Schema 强约束，见 docs/07 手册 同款四层防御链风格）
_LLM_SYSTEM_PROMPT = (
    "你是电商客诉意图分类器。只输出 JSON，不要输出任何解释文字。"
    '格式：{"intent": "REFUND|QUERY|EVIDENCE|APPEAL|HUMAN|COMPLAINT_ESCALATE",'
    ' "confidence": 0.0, "reason": "一句话理由"}。'
    "规则：intent 只能在上述 6 个枚举值中选一个；无法判断时选 HUMAN（人工接管）。"
)

_LLM_INTENT_SET = "|".join((
    INTENT_REFUND, INTENT_QUERY, INTENT_EVIDENCE, INTENT_APPEAL,
    INTENT_HUMAN, INTENT_COMPLAINT_ESCALATE,
))

# LLM 层 img/label 输出中的意图字段枚举校验
_LLM_VALID = re.compile(rf"^\s*({_LLM_INTENT_SET})\s*$")

# Fail-Safe 最终意图
_FALLBACK_INTENT = INTENT_HUMAN

# 规则层置信度：正则命中置信（可被测试依赖的稳定常量）
RULE_CONFIDENCE_HIGH = 0.95
RULE_CONFIDENCE_MEDIUM = 0.80
RULE_CONFIDENCE_WEAK = 0.70

# LLM 层允许的最大重试次数（含首次；外加一次 JSON 修复）
_MAX_LLM_ATTEMPTS = 2

# 多意图冲突阈值（V2.0 -4）：次高意图置信达到此值视同多意图冲突，须记录候选
MULTI_CONFLICT_THRESHOLD = 0.50

# 混合来源（hybrid）触发线：规则命中但置信低于 0.85 且 LLM 可用时交叉验证
HYBRID_VERIFY_BELOW = 0.85


@dataclass
class MultiIntentAnalysis:
    """多意图拆分 + 冲突检测结果（V2.0 -4）。

    candidates 按置信降序（各意图独立命中词，可同文本多意图）；conflict 表示存在
    两个以上意图且次高置信达标 —— 此时该文本无法唯一判定，须记录供人工/评测复核。
    """
    candidates: list[dict] = field(default_factory=list)   # [{intent, confidence, hits, matched}]
    conflict: bool = False
    primary: str | None = None

    def as_dict(self) -> dict:
        return {"conflict": self.conflict, "primary": self.primary,
                "candidates": list(self.candidates)}


@dataclass
class IntentRouteResult:
    """一次意图路由的完整证据（可落库到 intent_results）。"""
    final_intent: str
    confidence: float
    risk: str                      # LOW / MEDIUM
    level_1_rule: str | None = None
    level_2_llm: str | None = None
    raw_sha256: str = ""
    reason: str = ""
    retries_used: int = 0          # LLM 层实际用到几轮（0 = 规则层直接命中）
    # V2.0 -4：多意图候选 + 来源（rule/llm/hybrid）+ 混合仲裁意见
    multi_intents: list[dict] = field(default_factory=list)
    route_source: str | None = None
    combined_intent: str | None = None

    def as_dict(self) -> dict:
        d = {
            "final_intent": self.final_intent,
            "confidence": self.confidence,
            "risk": self.risk,
            "level_1_rule": self.level_1_rule,
            "level_2_llm": self.level_2_llm,
            "raw_sha256": self.raw_sha256,
            "reason": self.reason,
            "retries_used": self.retries_used,
        }
        if self.multi_intents:
            d["multi_intents"] = list(self.multi_intents)
        if self.route_source:
            d["route_source"] = self.route_source
        if self.combined_intent:
            d["combined_intent"] = self.combined_intent
        return d


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# =====================================================================
# V3-4 意图模型治理：分类器独立版本导出 + 混合路由 Token 对比
# =====================================================================

# 规则集版本（内容指纹由 sha256(snapshot) 计算；每次规则变更须升版并走发布门禁）
RULE_SET_VERSION = "v3.4.0"

# 轻量模型：当前版本未实现独立轻量模型（诚实声明，绝不冒充已训练模型）
_LIGHTWEIGHT_DESCRIPTOR = {
    "implemented": False,
    "note": "未实现独立轻量分类模型；混淆消解由规则层派生描述符承担",
    "derived_from": "rule",
}

# 混合路由 Token 节省目标（实测口径）
TOKEN_GAIN_TARGET = 0.40


def classifier_snapshot(kind: str) -> dict:
    """各分类器的**独立版本快照**（只导出、不落密钥）。

    - rule / dict：导出规则集 + 版本号（同一份规则承担词典职能，语义如实标注）；
    - lightweight：诚实声明未实现独立轻量模型（描述符）；绝不伪造模型指纹；
    - llm：导出系统提示词文本 + 模型名（不落 API_KEY / 任何密钥材料）。

    快照内容直接滚 sha256 指纹供 version 注册中心留痕；本函数纯导出，不改任何状态。
    """
    from app.core.config import settings

    if kind == "rule":
        snapshot = {"rules": _RULES, "confidence": {
            "high": RULE_CONFIDENCE_HIGH, "medium": RULE_CONFIDENCE_MEDIUM,
            "weak": RULE_CONFIDENCE_WEAK}}
    elif kind == "dict":
        # 词典 = 规则层的词面集合（当前实现共用；语义标注以防误读）
        snapshot = {"dictionary": {k: list(v) for k, v in _RULES.items()},
                    "note": "词面与规则层共用同源词条（V3-4 如实标注）"}
    elif kind == "lightweight":
        snapshot = dict(_LIGHTWEIGHT_DESCRIPTOR)
    elif kind == "llm":
        snapshot = {
            "system_prompt": _LLM_SYSTEM_PROMPT,
            "model": settings.LLM_MODEL,
            "fallback_model": settings.LLM_FALLBACK_MODEL,
            "max_attempts": _MAX_LLM_ATTEMPTS,
            "note": "快照不含 API_KEY 等密钥材料",
        }
    else:
        raise ValueError(f"非法分类器种类: {kind}（须在 rule/dict/lightweight/llm）")

    return {
        "classifier": kind,
        "version": RULE_SET_VERSION,
        "content_sha256": sha256_hex(json.dumps(snapshot, ensure_ascii=False,
                                                sort_keys=True)),
        "snapshot": snapshot,
    }


def _estimate_llm_tokens(prompt: str) -> int:
    """本地令牌估算（字节长度近似，与项目 estimate_tokens 同思路）。

    仅用于**相对**对比（同一提示词在 pure/hybrid 两路同口径计数）；真实云端
    usage 未接通时如实标注 measured=False，绝不伪造绝对值。
    """
    length = len((prompt or "").encode("utf-8"))
    return max(1, length // 2)


def measure_hybrid_token_gain(corpus: list[str], *, token_fee=None) -> dict:
    """混合路由 vs 纯 LLM 的 Token 节省（实测抽样）。

    对同一批文本做两种计费模拟：
    - pure_llm：每条文本都走 LLM 触发（关键词均可触发 LLM）的 token 之和；
    - hybrid：与 route_intent 同语义——规则层高置信命中（confidence >=
      HYBRID_VERIFY_BELOW）不触发 LLM（0 token）；否则才计 LLM token。

    token_fee：可注入的计费函数 prompt->token 数（测试用确定性计数器）；
    缺省用本地字节近似。结论 truthly 标注：本地近似 = measured(estimate)，
    云端 usage 计费未测得时如实注明。
    """
    pure_llm = 0
    hybrid = 0
    llm_hits = 0
    for text in corpus:
        rule_hit = _match_rule(text)
        # pure 路径：无论规则是否命中都走 LLM（系统提示+用户提示）
        pure_prompt = _llm_prompt(text, rule_hit)
        pure_llm += _token_of(token_fee, pure_prompt)
        # hybrid 路径：规则高置信命中 -> 不触发 LLM
        if rule_hit is not None and _confidence_for(rule_hit, text) >= HYBRID_VERIFY_BELOW:
            hybrid += 0
        else:
            hybrid += _token_of(token_fee, _llm_prompt(text, rule_hit))
            llm_hits += 1
    ratio = (hybrid / pure_llm) if pure_llm else 0.0
    gain = 1.0 - ratio
    return {
        "corpus_n": len(corpus),
        "pure_llm_tokens": pure_llm,
        "hybrid_tokens": hybrid,
        "token_ratio": round(ratio, 4) if pure_llm else None,
        "token_gain": round(gain, 4) if pure_llm else None,
        "llm_hits": llm_hits,
        "meets_target": bool(pure_llm and gain >= TOKEN_GAIN_TARGET),
        "measured": True,
        "measurement": "estimate（字节近似）",
    }


def _token_of(token_fee, prompt: str) -> int:
    if token_fee is not None:
        return int(token_fee(prompt))
    return _estimate_llm_tokens(prompt)


def _match_rule(text: str) -> str | None:
    """规则层：顺序扫关键字，返回命中的意图；未命中返回 None。

    意图优先级处理：越俎意图（COMPLAINT_ESCALATE/HUMAN/APPEAL）靠后，
    避免"退款"等常见词被投诉词提前抢走。若多意图命中取优先级最高的那个。
    """
    # 优先级：越俎意图优先（尝到投诉/人工就越早转人工），其次退款
    priority = [
        INTENT_HUMAN, INTENT_COMPLAINT_ESCALATE, INTENT_APPEAL,
        INTENT_REFUND, INTENT_EVIDENCE, INTENT_QUERY,
    ]
    hits = []
    for intent, pats in _RULES.items():
        for pat in pats:
            if re.search(pat, text):          # 默认 re.IGNORECASE? 中文无大小写，含 ASCII 词其大小写语义不强
                hits.append(intent)
                break
    if not hits:
        return None
    for p in priority:
        if p in hits:
            return p
    return hits[0]


def _confidence_for(intent: str, text: str) -> float:
    """基于命中词数给置信度：仅 1 个词命中 -> 中置信，多词/强语义 -> 高置信。"""
    n = sum(1 for pat in _RULES.get(intent, []) if re.search(pat, text))
    if n >= 2:
        return RULE_CONFIDENCE_HIGH
    if intent in (INTENT_HUMAN, INTENT_COMPLAINT_ESCALATE, INTENT_APPEAL):
        return RULE_CONFIDENCE_MEDIUM     # 越俎意图宁可高一点回调
    return RULE_CONFIDENCE_MEDIUM


def analyze_multi_intents(text: str) -> MultiIntentAnalysis:
    """多意图拆分 + 冲突检测（V2.0 -4）。

    - 逐意图独立扫关键词（互不复用命中），各自统计命中词数 -> 候选列表，置信降序；
    - 一个文本可同时命中多意图（如"退款失败还要投诉转人工"）；
    - conflict=True 当「存在两个不同意图候选」且「次高置信 >= MULTI_CONFLICT_THRESHOLD」，
      表示无法唯一判定 —— 记入 conflict_candidates 供人工/评测复核，绝不静默丢弃。
    """
    text = text or ""
    cands: list[dict] = []
    for intent, pats in _RULES.items():
        matched = [p for p in pats if re.search(p, text)]
        if matched:
            cands.append({
                "intent": intent,
                "confidence": _confidence_for(intent, text),
                "hits": len(matched),
                "matched": matched[:3],
            })
    cands.sort(key=lambda c: c["confidence"], reverse=True)
    primary = cands[0]["intent"] if cands else None
    conflict = (
        len(cands) >= 2
        and cands[0]["intent"] != cands[1]["intent"]
        and cands[1]["confidence"] >= MULTI_CONFLICT_THRESHOLD
    )
    return MultiIntentAnalysis(candidates=cands, conflict=conflict, primary=primary)


def _conflict_payload(candidates: list[dict]) -> dict | None:
    """把候选列表转成落库 payload（conflict 标志由次高置信阈值重算，不含原文敏感词）。"""
    if not candidates:
        return None
    conflict = (
        len(candidates) >= 2
        and candidates[0]["intent"] != candidates[1]["intent"]
        and candidates[1]["confidence"] >= MULTI_CONFLICT_THRESHOLD
    )
    return {"conflict": conflict, "primary": candidates[0]["intent"],
            "candidates": list(candidates)}


def _extract_intent(out: dict | None) -> str | None:
    """从 LLM 回调输出中取校验过的合法意图（兼容 content / 直接 intent 两种形状）。"""
    if not isinstance(out, dict):
        return None
    if isinstance(out.get("content"), str):
        intent = _parse_llm(out["content"])
        if intent is not None:
            return intent
    candidate = out.get("intent")
    if isinstance(candidate, str) and _LLM_VALID.match(candidate):
        return candidate.strip()
    return None


def _parse_llm(raw: str) -> str | None:
    """宽松解析 LLM 输出：取首个 { 到末尾 }，再校验 intent 枚举。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    intent = obj.get("intent") if isinstance(obj, dict) else None
    if isinstance(intent, str) and _LLM_VALID.match(intent):
        return intent.strip()
    return None


def _json_repair_once(raw: str) -> str:
    """对截断/多余的 JSON 做一次轻量修复：补全结尾括号、剔除尾部废话。

    修复策略（保守，不做格式重排）：
    - 如果以 `{` 开头但没有闭合 `}`，在字符串末尾尝试补齐 JSON 容器；
    - 去掉文本首尾非 JSON 的填充字符（引号不补，避免猜错字段语义）。
    """
    text = (raw or "").strip()
    if not text:
        return text
    # 以 `{` 开头但没有 `}`：尝试补一个 `}`（多数截断发生在对象结尾）
    if text.startswith("{") and "}" not in text:
        return text + "}"
    return text


def _llm_prompt(text: str, candidate: str | None) -> str:
    base = f"请把以下客诉内容归类：\n{text[:2000]}"
    if not candidate:
        return base
    return (f"{base}\n（提示：规则层疑似 [ {candidate} ]，请校验或纠正，"
            "仍只输出合规 JSON。）")


def route_intent(
    db: Session,
    *,
    case_id: str,
    text: str,
    llm_route: Callable[[str], dict | None] | None = None,
    persist: bool = True,
) -> IntentRouteResult:
    """两级意图路由。

    - 规则层命中 -> level_1_rule 非空、level_2_llm 为 None，直接定意图；
    - 规则层未命中 -> 走 LLM 层（若注入 `llm_route`），输出一次修复+有限重试；
    - LLM 不可用 / 输出不合法 -> level_2_llm=None，Fail-Safe 兜底 HUMAN，绝不返回 UNKNOWN 给上游。

    `llm_route(prompt) -> dict`：供外部注入（真实 LLM / Mock）。dict 形如
    {"content": str} 或 {"intent": str, "confidence": float}。返回 None 视为不可用。

    `persist=False` 时只返回判定证据，不写库（纯函数侧测试用）。
    """
    text = text or ""
    raw_hash = sha256_hex(text)

    rule_hit = _match_rule(text)
    if rule_hit is not None:
        multi = analyze_multi_intents(text)
        confidence = _confidence_for(rule_hit, text)
        # 混合来源（V2.0 -4）：规则弱命中 + LLM 可用 -> 交叉验证。
        # LLM 给出不同合法意图 -> combined 记录、route_source=hybrid；最终仍按规则保守
        # 路由（不抢判定），分歧点留作线上误路由候选（行内进反馈采样）。
        combined: str | None = None
        source = "rule"
        if llm_route is not None and confidence < HYBRID_VERIFY_BELOW:
            try:
                other = _extract_intent(llm_route(_llm_prompt(text, rule_hit)))
                if other is not None and other != rule_hit:
                    combined = other
                    source = "hybrid"
            except Exception as exc:  # noqa: BLE001 —— 交叉验证失败不得影响规则判定
                logger.warning("intent hybrid cross-check failed: %s", exc)
        reason = f"rule layer hit {rule_hit}"
        if source == "hybrid":
            reason += f" (llm cross-check diverges -> {combined})"
        res = IntentRouteResult(
            final_intent=rule_hit,
            confidence=confidence,
            risk="LOW",
            level_1_rule=rule_hit,
            level_2_llm=combined,
            raw_sha256=raw_hash,
            reason=reason,
            retries_used=0,
            multi_intents=multi.candidates,
            route_source=source,
            combined_intent=combined,
        )
        if persist:
            _persist(db, case_id=case_id, res=res)
        return res

    # 规则层未命中 -> LLM 层
    level2: str | None = None
    reason = ""
    retries = 0
    if llm_route is not None:
        for attempt in range(_MAX_LLM_ATTEMPTS):
            retries = attempt + 1
            try:
                out = llm_route(_llm_prompt(text, rule_hit))
                if out is None:
                    continue
                content = out.get("content") if isinstance(out, dict) else None
                intent = _parse_llm(content or "")
                if intent is None and isinstance(out, dict):
                    # 直接给 {"intent": "..."} 的结构（Mock / 简化注入）
                    candidate = out.get("intent")
                    if isinstance(candidate, str) and _LLM_VALID.match(candidate):
                        intent = candidate.strip()
                if intent is not None:
                    level2 = intent
                    break
                # 输出不合法：尝试一次修复后重试
                if attempt == 0:
                    fixed = _json_repair_once(content or "")
                    intent = _parse_llm(fixed)
                    if intent is not None:
                        level2 = intent
                        break
            except Exception as exc:  # noqa: BLE001 —— LLM 层任何异常都不得外泄
                logger.warning("intent llm layer failed (attempt %d): %s", attempt + 1, exc)
                continue
        reason = f"llm layer intent={level2 or 'unavailable'}"
    else:
        reason = "no llm provider; rule layer miss -> fallback"

    final_intent = level2 if level2 in VALID_INTENTS else _FALLBACK_INTENT
    confidence = 0.55 if level2 else 0.30
    risk = "LOW" if level2 else "MEDIUM"

    res = IntentRouteResult(
        final_intent=final_intent,
        confidence=confidence,
        risk=risk,
        level_1_rule=rule_hit,           # 此处恒 None（规则层未命中才走到这）
        level_2_llm=level2,
        raw_sha256=raw_hash,
        reason=reason,
        retries_used=retries,
        # 来源标记：走到这里必是规则未命中；注入了 LLM 即"llm"来源，否则无 LLM 强路由
        route_source="llm" if llm_route is not None else None,
    )
    if persist:
        _persist(db, case_id=case_id, res=res)
    return res


def _persist(db: Session, *, case_id: str, res: IntentRouteResult) -> None:
    """落库 intent_results：只存 sha256，不落原文；违反即审计异常。"""
    row = IntentResult(
        id=new_uuid(),
        case_id=case_id,
        level_1_rule=res.level_1_rule,
        level_2_llm=res.level_2_llm,
        final_intent=res.final_intent,
        confidence=res.confidence,
        raw_sha256=res.raw_sha256,
        risk=res.risk,
        conflict_candidates=_conflict_payload(res.multi_intents),
        route_source=res.route_source,
    )
    db.add(row)
    db.commit()


def record_intent_from_rule(db: Session, *, case_id: str, text: str) -> IntentRouteResult:
    """规则层直接落 intent_results（intake 建单前调用，不注入 LLM）。"""
    return route_intent(db, case_id=case_id, text=text, llm_route=None, persist=True)


def build_llm_route(
    system_prompt: str | None = None,
    *,
    max_attempts: int = _MAX_LLM_ATTEMPTS,
    backoff_base: float = 0.5,
    backoff_max: float = 4.0,
) -> Callable[[str], dict | None]:
    """LLM 意图网关（V2.0 -4）：云 API 主 + 熔断 + Ollama 回退 + 指数退避。

    返回 `route_intent` 可直接注入的 `llm_route` 回调，输出契约 {"content": str,
    "provider": "cloud"|"ollama"}；两 Provider 全挂返回 None -> 上层 Fail-Safe HUMAN，
    绝不抛错扩散（与模块红线一致）。

    退避：云不重试；Ollama 每轮尝试前 `backoff_base * 2**(attempt-1)`（0.5s, 1s, 2s …，
    封顶 backoff_max）。Provider 懒创建，避免仅 import 即建连接。
    """
    from app.infrastructure import llm as _llm_infra

    sp = system_prompt or _LLM_SYSTEM_PROMPT
    breaker = _llm_infra.CircuitBreaker()
    cloud = _llm_infra.CloudLLMProvider(system_prompt=sp)
    ollama = _llm_infra.OllamaProvider(system_prompt=sp)

    def _call_with_backoff(provider, prompt: str, step: int) -> str | None:
        if step > 0:
            time.sleep(min(backoff_max, backoff_base * (2 ** (step - 1))))
        try:
            return provider.chat_json(prompt)
        except Exception as exc:  # noqa: BLE001 —— 任一 Provider 异常只退回下一源
            logger.warning("intent llm gateway attempt %d failed: %s", step + 1, exc)
            return None

    def llm_route(prompt: str) -> dict | None:
        # 第 1 源：云 API（先过熔断闸门；失败计错，可能触发 OPEN）
        if breaker.allow():
            content = _call_with_backoff(cloud, prompt, 0)
            if content is not None:
                breaker.record(True)
                return {"content": content, "provider": "cloud"}
            breaker.record(False)
        # 第 2 源：Ollama 回退（每次尝试前指数退避，至多 max_attempts 轮）
        for step in range(1, max_attempts + 1):
            content = _call_with_backoff(ollama, prompt, step)
            if content is not None:
                breaker.record(True)
                return {"content": content, "provider": "ollama"}
        breaker.record(False)
        return None

    return llm_route


# =====================================================================
# 线上误路由 / 人工纠正 -> 评测候选集（V2.0 -4 / intent_feedback_samples）
# 生命周期：PENDING -> REVIEWED -> ADOPTED(进候选集)/ REJECTED。
# ADOPTED 只在黄金集重建时消费（eval_center.create_suite_from_golden 走既有冻结流程），
# 绝不自动改线上路由或冻结 Suite（V-R1 红线保持）。
# =====================================================================

FB_PENDING = "PENDING"
FB_REVIEWED = "REVIEWED"
FB_ADOPTED = "ADOPTED"
FB_REJECTED = "REJECTED"

_FB_STATES = frozenset({FB_PENDING, FB_REVIEWED, FB_ADOPTED, FB_REJECTED})


def record_feedback_sample(
    db: Session, *,
    case_id: str | None, route_final_intent: str,
    human_corrected_intent: str, reason: str | None = None,
) -> IntentFeedbackSample:
    """落一条线上反馈采样（PENDING）。intent 校验硬约束：意图合法才可入。"""
    if route_final_intent not in VALID_INTENTS or human_corrected_intent not in VALID_INTENTS:
        raise ValueError("非法意图值，反馈采样拒绝记录")
    row = IntentFeedbackSample(
        id=new_uuid(), case_id=case_id,
        route_final_intent=route_final_intent,
        human_corrected_intent=human_corrected_intent,
        reason=(reason or "")[:200], status=FB_PENDING,
    )
    db.add(row)
    db.commit()
    return row


def list_feedback_samples(db: Session, *, status: str | None = None,
                          limit: int = 50) -> list[dict]:
    if status and status not in _FB_STATES:
        raise ValueError(f"非法反馈状态: {status}")
    q = db.query(IntentFeedbackSample)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(IntentFeedbackSample.created_at.desc()).limit(limit).all()
    return [_fb_as_dict(r) for r in rows]


def review_feedback_sample(
    db: Session, sample_id: str, *,
    decision: str, reviewed_by: str, reason: str | None = None,
) -> dict:
    """审核流转：PENDING->REVIEWED->(ADOPTED|REJECTED)。非法跳级即异常。"""
    if decision not in _FB_STATES:
        raise ValueError(f"非法审核决策: {decision}")
    row = db.get(IntentFeedbackSample, sample_id)
    if row is None:
        raise KeyError(f"反馈采样 {sample_id} 不存在")
    if decision == FB_REVIEWED:
        if row.status != FB_PENDING:
            raise ValueError(f"非法跳级：{row.status} -> {decision}")
    else:
        if row.status != FB_REVIEWED:
            raise ValueError(f"非法跳级：必须先 REVIEWED 后才能 {decision}")
        if reason:
            row.reason = (reason or "")[:200]
    row.status = decision
    row.reviewed_by = reviewed_by
    db.commit()
    return _fb_as_dict(row)


def export_feedback_candidates(db: Session) -> list[dict]:
    """导出 ADOPTED 候选（供评测黄金集重建/复核）：案号 + 机器意图 + 人工纠正意图。"""
    rows = (db.query(IntentFeedbackSample)
            .filter_by(status=FB_ADOPTED)
            .order_by(IntentFeedbackSample.created_at.desc()).all())
    return [_fb_as_dict(r) for r in rows]


def _fb_as_dict(r: IntentFeedbackSample) -> dict:
    return {
        "id": r.id,
        "case_id": r.case_id,
        "route_final_intent": r.route_final_intent,
        "human_corrected_intent": r.human_corrected_intent,
        "reason": r.reason,
        "status": r.status,
        "reviewed_by": r.reviewed_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }