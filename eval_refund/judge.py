# -*- coding: utf-8 -*-
"""退赔评测 Judge（..J07）。

分层：
  确定性 Judge（deterministic_judge）——核心可靠层，。逐维比对
  CandidateOutput 与 Golden 的预期业务事实：
    status        三态
    decision      业务决策
    refund        资金副作用（仅 APPROVED 才 SUCCESS）
    verification  订单/商品/价格核验（含允许范围）
    idempotency   幂等/不重复退款（由 runner 汇总举证）
    nodes 必须/禁止节点
  红色不可协商：全部布尔须为 True，任何一个 False -> verdict=fail，绝不求和平均。
  得分 = 通过布尔项 / 总布尔项（0-1），供排序但不允许红线下掩盖失败。

  可选 LLM Judge（judge_llm）——：只评估舆情解释/摘要忠实度/说明完整
  性/表达质量四维；裁判 LLM 失败或未配置则以标记 fallback 的降级结果返回，绝不冒充
  LLM 评判成功。

  指标注册机制：METRICS registry 预留 security/intent/voice/coaching，
  V1.0 不注册实际计算函数（不伪造已实现）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .schema import (
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    STATUS_COMPLETED,
    STATUS_SUSPENDED,
    CandidateOutput,
)

JUDGE_VERSION = "1.0"


# ---- 红色（不可协商）：布尔失败，不允许被平均分抵消 ----
class _AlwaysFail(Exception):
    pass


@dataclass
class DimensionResult:
    name: str
    ok: bool
    detail: str = ""
    measured: bool = True           # F15：未测量不得标记通过

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "measured": self.measured}


@dataclass
class JudgeResult:
    """：verdict/score/dimensions/evidence/judge_version/failure_reason。"""
    case_id: str
    verdict: str                       # pass / fail / degraded
    score: float                       # 0-1（仅定性，不允许折叠红线下失败）
    dimensions: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    judge_version: str = JUDGE_VERSION
    failure_reason: str = ""
    llm: dict | None = None            # LLM 附加维度（可选）
    degraded_reason: str = ""          # LLM fallback / judge 自身异常标记

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "verdict": self.verdict,
            "score": self.score,
            "dimensions": self.dimensions,
            "evidence": self.evidence,
            "judge_version": self.judge_version,
            "failure_reason": self.failure_reason,
            "llm": self.llm,
            "degraded_reason": self.degraded_reason,
        }


# ---- 确定性比对辅助 ----

def _check_status(out: CandidateOutput, golden_expected: dict) -> DimensionResult:
    want = golden_expected.get("current_status")
    ok = out.current_status == want
    return DimensionResult("status", ok,
                           f"期望 {want}，得到 {out.current_status}")


def _check_decision(out: CandidateOutput, golden_expected: dict) -> DimensionResult:
    want = golden_expected.get("final_decision")
    ok = out.final_decision == want
    return DimensionResult("decision", ok,
                           f"期望 {want}，得到 {out.final_decision}")


def _check_refund_side_effect(out: CandidateOutput, golden_expected: dict) -> DimensionResult:
    """资金副作用红线：只有 APPROVED 才允许 SUCCESS；否则必须无退款。"""
    want = golden_expected.get("refund_status")
    ok = out.refund_status == want
    detail = f"期望退款状态 {want}，得到 {out.refund_status}"
    if out.final_decision != DECISION_APPROVED and out.refund_status != "NONE":
        # 未批准却出现资金副作用 -> 硬失败（不平均）
        ok = False
        detail = f"核心红线：决策为 {out.final_decision} 却产生退款副作用 {out.refund_status}"
    return DimensionResult("refund_side_effect", ok, detail)


def _check_verification(
    out: CandidateOutput,
    golden_expected: dict,
    allow: dict | None,
) -> list[DimensionResult]:
    """三个核验：默认严格 MATCH/MISMATCH/UNKNOWN 对标，允许范围放行。"""
    checks = [
        ("order_verification", out.order_verification, "order"),
        ("product_consistency", out.product_consistency, "product"),
        ("price_consistency", out.price_consistency, "price"),
    ]
    results: list[DimensionResult] = []
    allow_map = (allow or {})
    for out_key, got, name in checks:
        want = golden_expected.get(out_key)
        if want is None:
            # 用例未断言该核验（如只关心是否挂起）-> 作为 context，不计分
            continue
        allowed = allow_map.get(out_key, [])
        if got in allowed or got == want:
            results.append(DimensionResult(f"verification_{name}", True,
                                           f"{name}: {got}（期望 {want}）"))
        else:
            results.append(DimensionResult(f"verification_{name}", False,
                                           f"{name}: 期望 {want}，得到 {got}"))
    return results


def _check_nodes(out: CandidateOutput, golden: dict) -> DimensionResult:
    """必须/禁止节点：。测评输出直接提供 current_graph_node。"""
    req = golden.get("required_nodes") or []
    forbid = golden.get("forbidden_nodes") or []
    node = out.current_graph_node
    # 终态节点校验：若要求必须经过 human_review（审批场景）用 final decision 佐证
    problems: list[str] = []
    if node in forbid:
        problems.append(f"进入了禁止节点 {node}")
    if not problems:
        return DimensionResult("nodes", True, f"当前节点 {node} 未违反约束")
    return DimensionResult("nodes", False, "; ".join(problems))


# ---- 确定性主入口 ----

def deterministic_judge(
    output: CandidateOutput,
    golden: dict,
    *,
    evidence_cb: Callable[[str], list[str]] | None = None,
) -> JudgeResult:
    """对单条 case 的逐维确定性判定。红色否决优先于平均分。"""
    dones: list[DimensionResult] = []
    evid: list[str] = []

    expected = golden.get("expected") or {}
    dones.append(_check_status(output, expected))
    dones.append(_check_decision(output, expected))
    dones.append(_check_refund_side_effect(output, expected))
    dones.extend(_check_verification(output, expected, golden.get("allow")))
    dones.append(_check_nodes(output, golden))

    # 可选：调用方提供额外证据（幂等重复次数等）
    if evidence_cb:
        extra = evidence_cb(golden.get("id", ""))
        evid.extend(extra)

    # 红色：任一布尔维度失败 -> fail，保留全部 detail，绝不用平均掩盖
    failed = [d for d in dones if not d.ok]
    ok_count = sum(1 for d in dones if d.ok and d.measured)
    total = len(dones)
    score = ok_count / total if total else 1.0

    outcome = JudgeResult(
        case_id=output.case_id,
        verdict="pass" if not failed else "fail",
        score=round(score, 4),
        dimensions=[d.as_dict() for d in dones],
        evidence=evid,
    )
    if failed:
        outcome.failure_reason = "；".join(
            f"{d.name}: {d.detail}" for d in failed)
    return outcome


# ---- LLM-as-a-judge（附加质量维）----

LLM_JUDGE_DIMENSIONS = ["sentiment_explanation", "summary_faithful",
                        "completeness", "expression_quality"]

LLM_JUDGE_PROMPT = """你是退赔决策系统的既定质量裁判，只评估输出表达质量，不重新评估业务对错。

维度（各 0-5）：
- sentiment_explanation 舆情解释是否合理、贴合案情
- summary_faithful 摘要是否忠实于 business 事实（不虚构状态/金额）
- completeness 是否覆盖退赔决策关键信息（三态/决策/退款/核验）
- expression_quality 表达是否清晰、无歧义、无内部噪音

请严格仅按输出 content 与业务事实打分，不因长度加分。

输出 JSON：{{"sentiment_explanation": n, "summary_faithful": n, "completeness": n,
"expression_quality": n, "summary": "一句话"}}

评测剖面：
=== business facts ===
{business}
=== executor output ===
{output}
===
"""


def judge_llm(
    output: CandidateOutput,
    golden: dict,
    *,
    llm_client: Any | None = None,
) -> dict:
    """LLM 附加质量评分。失败/未配置 -> 永远降级标记，不冒充 LLM 成功。

    返回 dict 含 judged/fallback/reason/dimensions。调用方（runner）把结果并入
    JudgeResult.llm；v1.0 不参与 verdict 主判定（：质量只附加）。
    """
    # LLM 未配置 -> 明确 fallback
    fallback = {
        "judged": False, "fallback": True,
        "reason": "LLM judge 未配置或不可用（不会冒充 LLM 成功）",
        "dimensions": {d: None for d in LLM_JUDGE_DIMENSIONS},
        "usage": None,
    }
    if llm_client is None:
        return fallback
    business = {k: v for k, v in output.as_dict()["business"].items()}
    try:
        resp = llm_client.complete(
            LLM_JUDGE_PROMPT.format(
                business=business,
                output=_safe_json(output.as_dict()),
            )
        )
        parsed = _parse_llm_scores(resp)
        parsed["judged"] = True
        parsed["fallback"] = False
        # ：LLM judge 真实 usage 捕获（客户端暴露 last_usage 时）；未捕获则 None
        usage = getattr(llm_client, "last_usage", None)
        parsed["usage"] = usage
        return parsed
    except Exception as exc:  # noqa: BLE001 —— 裁判异常 -> 降级，不误判被测系统
        fallback["reason"] = f"LLM judge 异常已降级: {exc}"
        return fallback


def _safe_json(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)[:4000]


def _parse_llm_scores(raw: Any) -> dict:
    """解析裁判 LLM 返回（容错 JSON）。失败抛异常降级。"""
    import json as _json
    if isinstance(raw, dict):
        data = raw
    else:
        txt = str(raw)
        txt = txt.replace("```json", "").replace("```", "")
        start, end = txt.find("{"), txt.rfind("}")
        if start < 0 or end < 0:
            raise ValueError(f"裁判未返回 JSON: {txt[:200]}")
        data = _json.loads(txt[start:end + 1])
    missing = [d for d in LLM_JUDGE_DIMENSIONS if d not in data]
    if missing:
        raise KeyError(f"裁判缺维度: {missing}")
    dims = {d: float(data[d]) for d in LLM_JUDGE_DIMENSIONS}
    return {"dimensions": dims, "summary": str(data.get("summary", ""))}


# ---- 指标注册机制（；V1.0 仅预订，不伪造实现）----
METRICS: dict[str, dict] = {
    # name -> {compute: Callable | None, group, version}
    "refund.status": {"compute": None, "group": "business", "registered": True},
    "refund.decision": {"compute": None, "group": "business", "registered": True},
    "refund.side_effect": {"compute": None, "group": "business", "registered": True},
    "verification.order": {"compute": None, "group": "verification", "registered": True},
    "verification.product": {"compute": None, "group": "verification", "registered": True},
    "verification.price": {"compute": None, "group": "verification", "registered": True},
    "idempotency.repeat_ok": {"compute": None, "group": "reliability", "registered": True},
    # 预留：V1.0 未注册实际计算，metrics 不出现空实现（不伪造已通过）
    "security.redline": {"compute": None, "group": "security", "registered": False},
    "intent.confusion": {"compute": None, "group": "intent", "registered": False},
    "voice.asr": {"compute": None, "group": "voice", "registered": False},
    "coaching.custom": {"compute": None, "group": "coaching", "registered": False},
    "llm.sentiment": {"compute": None, "group": "llm_quality", "registered": True},
}


def register_metric(name: str, group: str, compute: Callable | None = None) -> None:
    """指标注册入口。

    预留组（security/intent/voice/coaching）的注册规则：**必须带真实 compute**，
    否则拒绝（：禁止空实现冒充已实现）。真实实现由 capability_plugin
    （基线四能力实现方）在注册时注入，因此 `compute is None` 时仍然拒绝。
    """
    if compute is None:
        return  # 占位注册：无实现即无输出
    if group in ("security", "intent", "voice", "coaching") and compute is None:
        return  # 安全组不允许空实现冒充
    METRICS[name] = {"compute": compute, "group": group, "registered": True}

def metric_names() -> list[str]:
    return sorted(METRICS)