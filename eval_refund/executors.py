# -*- coding: utf-8 -*-
"""评测侧进程内执行器（快速模式 / 测试 / Prompt A-B）。

GoldenFollowerExecutor：按 Golden 期望原样产出 CandidateOutput —— 零网络、
零资金副作用。只用于评测中心的「快速模式」与自动化测试；真实黑盒评测请使用
RefundSystemHttpTarget（见 target.py）。

PromptAwareExecutor（M14）：同业务输出（oracle）但按真实组合 prompt 估算
token/成本 —— 让 A-B 实验在同一数据集下实测「提示词版本 / 模型」带来的
成本差异。token 来自真实字节长度（estimate_tokens），成本来自模型定价表，
usage.measured=True；是估计值而非计费值，同口径可复现。

红线：executor 只产生候选输出，不直接改 RefundCase、不执行任何真实退款；
未实测（oracle 无 usage 时）保持 measured=False，绝不伪造。
"""
from __future__ import annotations

import math
import re

from .schema import STATUS_SUSPENDED, CandidateOutput

__all__ = ["GoldenFollowerExecutor", "PromptAwareExecutor", "estimate_tokens"]


# ---- 确定性 token 估算（A-B Δ 实测，非计费值）----

_CJK_RE = re.compile(r"[　-〿一-鿿＀-￯]")
_WS_RE = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """确定性 token 估算：中文/全角 ~0.9 token/字符，ASCII ~3.5 字符/token。

    只用于同一数据集、同组合方式下的相对比较（Δ 可复现可审计）；
    精度是估计，绝不作为真实计费依据。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(_WS_RE.sub("", text)) - cjk
    return int(math.ceil(cjk * 0.9 + other / 3.5))


class GoldenFollowerExecutor:
    """确定性执行器：读 golden 期望 -> 生成理想候选输出（“oracle”）。"""

    def __init__(self, delay_ms: float = 0.0,
                 overrides: dict[str, CandidateOutput] | None = None) -> None:
        self.delay_ms = delay_ms
        self.overrides = overrides or {}
        self.calls: list[str] = []

    def __call__(self, case: dict, ctx: dict) -> CandidateOutput:
        self.calls.append(case["id"])
        ov = self.overrides.get(case["id"])
        if ov is not None:
            return ov
        exp = case.get("expected") or {}
        node = "human_review" if exp.get("current_status") == STATUS_SUSPENDED else "finalize"
        return CandidateOutput(
            case_id=case["id"],
            current_status=exp.get("current_status") or "COMPLETED",
            final_decision=exp.get("final_decision") or "APPROVED",
            refund_status=exp.get("refund_status") or "NONE",
            current_graph_node=node,
            order_verification=str(exp.get("order_verification") or ""),
            product_consistency=str(exp.get("product_consistency") or ""),
            price_consistency=str(exp.get("price_consistency") or ""),
            latency_ms=self.delay_ms,
        )


# ---- Prompt A-B（M14）：按真实组合 prompt 实测 token/成本 ----

def compose_prompt(prompt: str, case: dict) -> str:
    """按评测匿名化规则组合实际送入模型的 prompt（与 A-B 一致）。

    字段来源：order_id / requested_amount / reason / channel / tags / as_user
    （预留能力用例在 A-B 中 SKIPPED，不组合）。
    """
    parts = [prompt]
    parts.append(f"order_id={case.get('order_id') or ''}")
    parts.append(f"requested_amount={case.get('requested_amount') or ''}")
    parts.append(f"reason={case.get('reason') or ''}")
    parts.append(f"channel={case.get('channel') or ''}")
    tags = case.get("tags") or []
    parts.append(f"tags={','.join(str(t) for t in tags)}")
    parts.append(f"as_user={str(case.get('as_user') or '')}")
    return "\n".join(p for p in parts if p)


# 常见模型定价（BTO/百万 token；仅用于 A-B 成本估计，非计费）
_MODEL_RATES = {
    "claude-sonnet-4-5": {"in": 3.0, "out": 15.0},
    "claude-opus-4-5": {"in": 15.0, "out": 75.0},
    "gpt-4o-mini": {"in": 0.15, "out": 0.6},
    "gpt-4o": {"in": 2.5, "out": 10.0},
    "deepseek-v3": {"in": 0.27, "out": 1.1},
    "qwen-plus": {"in": 0.4, "out": 1.2},
}


class PromptAwareExecutor:
    """A-B 执行器：业务输出同 oracle，usage/token 按真实组合 prompt 估算。

    目的：把「Prompt 长短 / 模型定价」造成的成本差异隔离出来在相同数据集上
    对比 —— 业务判定不变（Golden 期望），delta 全来自 prompt/model。因此
    usage.measured=True 且 base 取自真实组合 prompt 的长度估计，非伪造值。

    不改变 business 字段（资金副作用安全——评测不触及真实退款）。
    """

    def __init__(self, prompt: str, model_name: str | None = None,
                 input_fallback: int = 0) -> None:
        self.prompt = prompt
        self.model_name = model_name or "claude-sonnet-4-5"
        self.input_fallback = input_fallback
        self.overrides: dict[str, CandidateOutput] = {}

    def __call__(self, case: dict, ctx: dict) -> CandidateOutput:
        ov = self.overrides.get(case["id"])
        composed = compose_prompt(self.prompt, case)
        input_tokens = estimate_tokens(composed) + self.input_fallback
        # 输出按 golden 期望答复估算（确定性、可复现）
        exp = case.get("expected") or {}
        output_text = " ".join(str(v) for v in exp.values())
        output_tokens = max(int(round(len(output_text) / 3.5)), 16)
        rates = _MODEL_RATES.get(self.model_name, {"in": 3.0, "out": 15.0})
        cost = (input_tokens / 1_000_000 * rates["in"]
                + output_tokens / 1_000_000 * rates["out"])
        out = ov or GoldenFollowerExecutor()(case, ctx)
        out.input_tokens = input_tokens
        out.output_tokens = output_tokens
        out.cost = round(cost, 6)
        out.measured = True
        # ：成本分账——usage 归属 agent=业务执行器 + 实际模型 + 费率快照
        out.agent = "business"
        out.model = self.model_name
        out.model_rates = dict(_MODEL_RATES.get(self.model_name,
                                                {"in": 3.0, "out": 15.0}))
        return out