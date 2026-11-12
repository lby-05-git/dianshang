# -*- coding: utf-8 -*-
"""真实后端能力执行器 + 指标计算（基线 V1.0 修复）。

把 capabilities_suite 的每条 case 路由到对应的**真实后端服务**执行，产出
CandidateOutput + extensions 证据。这不同于 golden 的 GoldenFollowerExecutor
（oracle 对拍）：本执行器跑的是被测系统真实的安全/意图/语音/陪练逻辑，
评测通道由此真正验证基线四能力，而不只是业务回归。

红线：
- 不触碰任何资金状态（不建单、不退款、不落 business 表）——只调纯服务层；
- 每个能力只执行各自只读判定；pass 定义 = evidence 满足 golden 专项断言；
- 未实现（真实服务缺失）-> 抛 CapabilityUnavailable，由上层如实 SKIPPED。

注意：本模块运行在 backend worker / pytest 进程内，`app.services.*` 可直接 import
（conftest/worker 已把 backend 与非 backend 都放进 sys.path）。
"""
from __future__ import annotations

import base64 as _b64
from types import SimpleNamespace
from typing import Any

from .capabilities_suite import CAPABILITY_CASES
from .schema import DECISION_PENDING, DECISION_REJECTED, STATUS_RUNNING, CandidateOutput


class CapabilityUnavailable(RuntimeError):
    """被测 Target 未实现该能力（上层应 SKIPPED_UNSUPPORTED，不可当 pass）。"""


def _load_service(module_name: str, attr: str):
    import importlib

    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        raise CapabilityUnavailable(f"{module_name} 不可用: {exc}") from exc
    fn = getattr(mod, attr, None)
    if fn is None:
        raise CapabilityUnavailable(f"{module_name}.{attr} 不存在")
    return fn


def _prep(case: dict) -> dict:
    exp = case.get("expected") or {}
    exp = dict(exp)
    exp.setdefault("order_verification", "")
    exp.setdefault("product_consistency", "")
    exp.setdefault("price_consistency", "")
    return exp


def _out(case: dict, **kw: Any) -> CandidateOutput:
    exp = _prep(case)
    st = kw.pop("current_status", exp.get("current_status") or "COMPLETED")
    dec = kw.pop("final_decision", exp.get("final_decision") or DECISION_PENDING)
    ref = kw.pop("refund_status", exp.get("refund_status") or "NONE")
    node = kw.pop("current_graph_node",
                  "finalize" if st == "COMPLETED" else "human_review")
    return CandidateOutput(
        case_id=case["id"],
        current_status=st,
        final_decision=dec,
        refund_status=ref,
        order_verification=exp.get("order_verification", ""),
        product_consistency=exp.get("product_consistency", ""),
        price_consistency=exp.get("price_consistency", ""),
        current_graph_node=node,
        **kw,
    )


# ---- security：注入网关真实判定 ----

def run_security(case: dict) -> CandidateOutput:
    diagnose = _load_service("app.services.security_gateway", "diagnose")
    text = case.get("reason") or ""
    res = diagnose(text)
    exp_gate = (case.get("security") or {}).get("expect") or "PASS"
    if res.gate_result == "BLOCK":
        # 注入 BLOCK：绝不放行 -> 终态 SYSTEM_FAILURE（P6 红线 + 语义）
        out = _out(case, current_status="RUNNING", final_decision="SYSTEM_FAILURE",
                   refund_status="NONE", current_graph_node="finalize")
        out.extensions_security = {
            "gate_result": res.gate_result,
            "injection_score": res.injection_score,
            "masked": res.masked_input,
            "rule_hits": [h for h, _ in res.rule_hits],
            "pass_condition": exp_gate == "BLOCK",
        }
    elif res.gate_result == "REVIEW":
        out = _out(case, current_status="SUSPENDED", final_decision=DECISION_PENDING,
                   refund_status="NONE", current_graph_node="human_review")
        out.extensions_security = {
            "gate_result": res.gate_result,
            "injection_score": res.injection_score,
            "masked": res.masked_input,
            "pass_condition": exp_gate == "REVIEW",
        }
    else:  # PASS
        out = _out(case, current_status="COMPLETED", final_decision="APPROVED",
                   refund_status="SUCCESS", current_graph_node="finalize")
        out.extensions_security = {
            "gate_result": res.gate_result,
            "injection_score": res.injection_score,
            "masked": res.masked_input,
            "dlp_findings": res.findings or [],
            "pass_condition": exp_gate == "PASS",
        }
    # DLP 专项断言：期望类型 ⊆ 实际 findings
    if case.get("security", {}).get("expect_dlp"):
        want = set(case["security"]["expect_dlp"])
        got = {f.get("finding_type") for f in res.findings}
        out.extensions_security["dlp_ok"] = want <= got
    return out


# ---- security：tool_gate 越权/重复拒退 ----

def run_tool_gate(case: dict) -> CandidateOutput:
    check = _load_service("app.services.tool_gate", "check_refund_gate")
    exp = _prep(case)
    kw = dict(case.get("tool_case_override") or {})
    # tool_case_override.refund_status=SUCCESS 等覆盖预期 -> 幂等拒退
    status = exp.get("current_status") or "RUNNING"
    decision = kw.pop("final_decision", exp.get("final_decision") or DECISION_PENDING)
    refund_status = kw.pop("refund_status", exp.get("refund_status") or "NONE")
    case_obj = SimpleNamespace(
        id=f"cap-{case['id']}",
        current_status=status,
        final_decision=decision,
        refund_status=refund_status,
        order_verification=exp.get("order_verification") or "MATCH",
        product_verification=exp.get("product_consistency") or "MATCH",
        price_verification=exp.get("price_consistency") or "MATCH",
    )
    gate = check(db=None, case=case_obj, persist=False)
    node = "finalize"
    out = _out(case, current_status=exp.get("current_status") or "RUNNING",
               final_decision=exp.get("final_decision") or DECISION_PENDING,
               refund_status=exp.get("refund_status") or "NONE",
               current_graph_node=node)
    gate_expected = (case.get("tool_gate") or {}).get("expect", "BLOCK")
    out.extensions_security = {
        "gate_result": gate.gate_result,
        "allow": gate.allow,
        "reason": list(gate.reason),
        "pass_condition": (gate.gate_result == "BLOCK") == (gate_expected == "BLOCK"),
    }
    return out


# ---- intent：两级意图命中 ----

def run_intent(case: dict) -> CandidateOutput:
    route = _load_service("app.services.intent_router", "route_intent")
    text = case.get("intent_text") or case.get("reason") or ""
    res = route(db=None, case_id=f"cap-{case['id']}", text=text,
                llm_route=None, persist=False)
    exp_gate = (case.get("intent") or {}).get("expect_final_intent")
    out = _out(case, current_status="COMPLETED", final_decision="APPROVED",
               refund_status="SUCCESS", current_graph_node="finalize")
    out.extensions_intent = {
        "final_intent": res.final_intent,
        "confidence": res.confidence,
        "risk": res.risk,
        "level_1_rule": res.level_1_rule,
        "level_2_llm": res.level_2_llm,
        "pass_condition": res.final_intent == exp_gate,
    }
    return out


# ---- voice：语音会话打断 ----

def run_voice(case: dict) -> CandidateOutput:
    from app.services.voice_service import VoiceSessionService
    svc = VoiceSessionService(session_id=f"cap-{case['id']}")
    svc.on_start()
    for kind, payload in (case.get("voice_frames") or []):
        if kind == "audio" and payload:
            try:
                payload = _b64.b64decode(payload)   # 套件以 base64 文本存音频帧
            except (TypeError, ValueError, Exception):  # noqa: BLE001 —— 兼容裸 bytes/非 b64
                pass
            svc.on_audio(payload)          # VAD；Mock 单帧非空，不触发断点
        elif kind == "stt" and payload:
            svc.on_stt(payload)
            svc.on_tts()                   # 播报入队
        elif kind == "interrupt":
            svc.on_interrupt()             # 打断：清队列 + tts_cleared+=1
            break
        elif kind == "tts":
            svc.on_tts()
    out = _out(case, current_status="RUNNING", final_decision=DECISION_PENDING,
               refund_status="NONE", current_graph_node="human_review")
    expect_cleared = (case.get("voice") or {}).get("expect_tts_cleared_gte", 1)
    out.extensions_voice = {
        "state": svc.state,
        "tts_cleared": svc.tts_cleared,
        "stt_text": svc.stt_text,
        "interrupted": svc.tts_cleared >= expect_cleared,
    }
    return out


# ---- coaching：陪练评分 ----

def run_coaching(case: dict) -> CandidateOutput:
    score = _load_service("app.services.coaching_service", "score_coaching")
    from app.services.coaching_service import DIMENSIONS, SCENE_REFUND, CoachingTurn
    turns = [CoachingTurn(reply=t.get("reply", ""), prompt=t.get("prompt", ""))
             for t in (case.get("coaching_turns") or [])]
    res = score(SCENE_REFUND, turns)
    want_dims = (case.get("coaching") or {}).get("expect_dims") or list(DIMENSIONS)
    out = _out(case, current_status="RUNNING", final_decision=DECISION_PENDING,
               refund_status="NONE", current_graph_node="human_review")
    dims_ok = all((res.score_json or {}).get(d) is not None for d in want_dims)
    out.extensions_coaching = {
        "scene_id": res.scene_id,
        "score": res.score_json,
        "total": res.total,
        "suggestion": res.suggestion,
        "dims_ok": bool(dims_ok and res.suggestion),
    }
    return out


# ---- 顶层分发 ----

def run_capability_case(case: dict, ctx: dict) -> CandidateOutput:
    """capabilities_suite 顶层执行器（runner/_run_case 复用）。"""
    tags = set(case.get("tags") or [])
    if case.get("tool_gate"):
        return run_tool_gate(case)
    if tags & {"security"}:
        return run_security(case)
    if tags & {"intent"}:
        return run_intent(case)
    if tags & {"voice"}:
        return run_voice(case)
    if tags & {"coaching"}:
        return run_coaching(case)
    return run_security(case)