# -*- coding: utf-8 -*-
"""可选 LLM-as-a-judge 客户端。

确定性 Judge 是 V1.0 主判定，不依赖本模块。LLM Judge 仅作附加质量维，且：
- 未配置 -> 明确 fallback（judge.py 标记 logged fallback），绝不冒充 LLM 成功；
- 配置后通过真实 LLM 评判，失败/超时也降级 fallback，不误判被测系统。

本模块是薄封装：注入任意满足 `complete(prompt) -> str` 的客户端即可。
配置示例（.env）：EVAL_LLM_API_KEY / EVAL_LLM_BASE_URL / EVAL_LLM_MODEL。
"""

from __future__ import annotations

import os

from .judge import judge_llm


def configured_llm_client() -> object | None:
    """若配置了评测专用 LLM 则返回客户端，否则 None（调用方走 fallback）。"""
    key = os.getenv("EVAL_LLM_API_KEY", "").strip()
    base = os.getenv("EVAL_LLM_BASE_URL", "").strip()
    model = os.getenv("EVAL_LLM_MODEL", "").strip()
    if not key or not base or not model:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=base)
        return _OpenAICompat(client, model)
    except Exception:  # noqa: BLE001 —— 依赖缺失即不可用，走 fallback
        return None


class _OpenAICompat:
    """OpenAI 兼容客户端适配（deepseek/qwen 等走 /v1/chat/completions）。

    usage 捕获：openai SDK 响应带 usage 字段时记入 last_usage
    （input/output tokens），供 judge 侧成本分账；SDK 未返回 / 无 usage 时保持 None，
    调用方如实记 judge 未测（measured=False），绝不伪造。
    """

    def __init__(self, client, model: str) -> None:
        self._client = client
        self._model = model
        self.last_usage: dict | None = None

    def complete(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=400,
        )
        # openai SDK：prompt_tokens=输入，completion_tokens=输出
        u = getattr(resp, "usage", None)
        if u is not None:
            self.last_usage = {
                "input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
            }
        else:
            self.last_usage = None
        return resp.choices[0].message.content or ""


def run_llm_judge(output, golden) -> dict:
    """runner 辅助：按环境检测注入 LLM judge，未配置自动 fallback。"""
    client = configured_llm_client()
    return judge_llm(output, golden, llm_client=client)