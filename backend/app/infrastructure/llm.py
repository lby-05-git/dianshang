# -*- coding: utf-8 -*-
"""Sentiment LLM 适配器（裁决 D2：云 API 主 + Ollama 回退 + 熔断器；docs/07 手册 ）。

四层防御链：JSON Schema 强约束 -> pydantic 严格校验 -> 提取修复/重试 1 次 ->
熔断降级 Ollama -> 可选本地可解释规则；生产默认保守判 HIGH（转人工）。
"""
import json
import logging
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings

logger = logging.getLogger("infra.llm")

_SYSTEM_PROMPT = (
    "你是电商客诉舆情分析助手。只输出 JSON，不要输出任何解释文字。"
    '格式：{"sentiment_level": "LOW|MEDIUM|HIGH", "keywords": ["关键词1", "关键词2"]}。'
    "LOW=情绪平和无舆情风险；MEDIUM=有明显不满；HIGH=强烈不满/可能发酵为舆情。"
)


class SentimentResult(BaseModel):
    sentiment_level: str = Field(pattern="^(LOW|MEDIUM|HIGH)$")
    keywords: list[str] = Field(default_factory=list, max_length=20)


_HIGH_RISK_TERMS = (
    "12315", "媒体", "记者", "曝光", "热搜", "全网", "直播", "消协", "监管部门",
)
_MEDIUM_RISK_TERMS = (
    "差评", "投诉", "维权", "欺骗", "骗子", "垃圾", "恶心", "气死", "太差", "非常失望",
)


def rule_based_sentiment(text: str) -> SentimentResult:
    """离线演示回退：只按公开、可解释的舆情关键词分级，不参与金额决策。"""
    normalized = (text or "").lower()
    high_hits = [term for term in _HIGH_RISK_TERMS if term.lower() in normalized]
    if high_hits:
        return SentimentResult(
            sentiment_level="HIGH", keywords=high_hits + ["local_rule_fallback"])
    medium_hits = [term for term in _MEDIUM_RISK_TERMS if term.lower() in normalized]
    if medium_hits:
        return SentimentResult(
            sentiment_level="MEDIUM", keywords=medium_hits + ["local_rule_fallback"])
    return SentimentResult(
        sentiment_level="LOW", keywords=["local_rule_fallback"])


class CircuitBreaker:
    """熔断器：CLOSED -> OPEN(30s) -> HALF_OPEN(探针) -> CLOSED/OPEN。"""

    def __init__(self, fail_threshold: int = 3, error_rate_threshold: float = 0.1,
                 window_seconds: int = 60, open_seconds: float = 30.0):
        self.fail_threshold = fail_threshold
        self.error_rate_threshold = error_rate_threshold
        self.window_seconds = window_seconds
        self.open_seconds = open_seconds
        self._state = "CLOSED"
        self._failures: list[float] = []
        self._calls: list[float] = []
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        if self._state == "OPEN" and time.monotonic() - self._opened_at > self.open_seconds:
            self._state = "HALF_OPEN"
        return self._state

    def allow(self) -> bool:
        self._prune()
        if self._state == "OPEN":
            return False
        if self._state == "HALF_OPEN":
            return True  # 放行探针
        if len(self._calls) >= 3 and self.error_rate() > self.error_rate_threshold:
            self._open()
            return False
        return True

    def record(self, ok: bool) -> None:
        self._prune()
        now = time.monotonic()
        self._calls.append(now)
        if not ok:
            self._failures.append(now)
            if self._state == "HALF_OPEN" or len(self._failures) >= self.fail_threshold:
                self._open()
        elif self._state == "HALF_OPEN":
            self._state = "CLOSED"
            self._failures.clear()

    def error_rate(self) -> float:
        if not self._calls:
            return 0.0
        return len(self._failures) / len(self._calls)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        self._calls = [t for t in self._calls if t > cutoff]
        self._failures = [t for t in self._failures if t > cutoff]

    def _open(self) -> None:
        self._state = "OPEN"
        self._opened_at = time.monotonic()
        logger.warning("LLM circuit breaker OPEN")


def _parse_json(text: str) -> dict:
    """宽松解析：取首个 { 到末尾 } 的子串（处理模型"话多"）。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json object in llm output")
    return json.loads(text[start:end + 1])


def _validate(raw: str) -> SentimentResult:
    return SentimentResult.model_validate(_parse_json(raw))


class CloudLLMProvider:
    """OpenAI 兼容云 API（agicto 中转）。

    `system_prompt` 可覆盖任务提示（意图/情绪等场景复用同一通道）；默认保持情绪语义。
    """

    def __init__(self, system_prompt: str | None = None):
        self._client = httpx.Client(timeout=settings.LLM_TIMEOUT_SECONDS)
        self._base = settings.LLM_API_BASE.rstrip("/")
        self._key = settings.LLM_API_KEY
        self._system = system_prompt or _SYSTEM_PROMPT

    def chat_json(self, user_text: str) -> str:
        resp = self._client.post(
            f"{self._base}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": settings.LLM_MODEL,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": user_text},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class OllamaProvider:
    """本地 Ollama 回退（`system_prompt` 可覆盖任务提示）。"""

    def __init__(self, system_prompt: str | None = None):
        self._client = httpx.Client(timeout=settings.LLM_TIMEOUT_SECONDS + 20)
        self._system = system_prompt or _SYSTEM_PROMPT

    def chat_json(self, user_text: str) -> str:
        resp = self._client.post(
            f"{settings.OLLAMA_BASE_URL}/api/chat",
            json={
                "model": settings.LLM_FALLBACK_MODEL,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": user_text},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


class SentimentProvider:
    """云 API 主 -> 熔断 -> Ollama -> 可配置规则回退/保守 HIGH。"""

    def __init__(self, breaker: CircuitBreaker | None = None):
        self._breaker = breaker or CircuitBreaker()
        self._cloud = CloudLLMProvider()
        self._ollama = OllamaProvider()

    def analyze(self, text: str) -> SentimentResult:
        if not text or not text.strip():
            return SentimentResult(sentiment_level="LOW", keywords=[])
        prompt = f"请分析以下客诉/聊天内容的情绪与舆情风险：\n{text[:2000]}"

        # 第1层：云 API（熔断闸门）
        if self._breaker.allow():
            result = self._try_chain(self._cloud, prompt)
            if result is not None:
                return result
            self._breaker.record(False)
        # 第2层：Ollama 回退
        result = self._try_chain(self._ollama, prompt)
        if result is not None:
            return result
        # 第3层：离线演示允许走可解释规则；真实生产默认仍保守转人工。
        if settings.SENTIMENT_RULE_FALLBACK_ENABLED:
            logger.warning("all LLM providers failed, using local rule fallback")
            return rule_based_sentiment(text)
        # 第4层：保守降级（HIGH -> 转人工）
        logger.warning("all LLM providers failed, conservative HIGH")
        return SentimentResult(sentiment_level="HIGH",
                               keywords=["llm_unavailable"])

    def _try_chain(self, provider, prompt: str) -> SentimentResult | None:
        """单供应商：调用 -> 校验 -> 失败修复/重试 1 次。"""
        for attempt in range(2):
            try:
                raw = provider.chat_json(prompt)
                result = _validate(raw)
                self._breaker.record(True)
                return result
            except (ValidationError, ValueError, json.JSONDecodeError, httpx.HTTPError) as exc:
                logger.warning("llm attempt %d failed: %s", attempt + 1, exc)
                if attempt == 0:
                    time.sleep(0.5)  # 重试 1 次前短等
        self._breaker.record(False)
        return None


class FakeSentimentProvider:
    """测试桩：固定低风险（128 元秒退场景）。"""

    def __init__(self, level: str = "LOW"):
        self._level = level

    def analyze(self, text: str) -> SentimentResult:
        return SentimentResult(sentiment_level=self._level, keywords=["fake"])


_provider = None


def get_sentiment_provider():
    global _provider
    if _provider is None:
        _provider = SentimentProvider()
    return _provider
