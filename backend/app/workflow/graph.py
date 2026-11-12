# -*- coding: utf-8 -*-
"""LangGraph 多Agent 工作流图（docs/06 ，docs/08 第5阶段）。

节点：START -> intake -> verification_stage -> multimodal evidence -> fraud -> sentiment -> amount_decision
      amount_decision --AUTO_APPROVE--> refund_execution -> finalize
      amount_decision --HUMAN_REVIEW--> human_review(interrupt 挂起)
      evidence --UNRELIABLE/FAILED/UNSAFE--> human_review（证据异常强制转人工）
      verification_stage: 订单真实性/商品一致性/价格一致性
      human_review --APPROVED--> refund_execution
      human_review --REJECTED--> finalize
      finalize: 按 state.decision 写 COMPLETED + 决策终态（状态机校验）

每个节点：写 agent_execution_logs + 更新 current_graph_node。
"""
import logging
import re
import time
from decimal import Decimal
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.config import settings
from app.domain.models import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_SUSPENDED,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    DECISION_SYSTEM_FAILURE,
    OCR_STATUS_FAILED,
    OCR_STATUS_SUCCESS,
    OCR_STATUS_UNRELIABLE,
    REFUND_STATUS_NONE,
    AgentExecutionLog,
    Evidence,
    RefundCase,
    SystemConfig,
    User,
)
from app.domain.state_machine import transition_case
from app.infrastructure.db import SessionLocal
from app.infrastructure.order_provider import MATCH, MISMATCH, get_order_provider
from app.workflow.state import GraphState

# 三个核验节点（verification_stage）内部按 step_order 分开留痕
from app.workflow.verification import run_verification

logger = logging.getLogger("workflow.graph")

# 节点 -> step_order（大屏轨迹排序）。
# 基线 V1.0：security_gate 不是业务节点（不打断业务 step 序列），anchor 在 intake(2)；
# 保持既有节点序号稳定（amount_decision=6），避免干扰既有轨迹断言/大屏渲染。
NODE_STEP = {
    "intake": 1, "verification": 2, "ocr": 3, "fraud": 4,
    "sentiment": 5, "amount_decision": 6, "human_review": 7,
    "refund_execution": 8, "finalize": 9,
}


# system_configs 读取缓存（任务四审计修复）：原实现每个图节点开一次 DB 会话查配置，
# 每案件 ~20 次 DB 往返拖慢 Worker 吞吐；改 30s TTL 缓存（D11 热改 30s 内生效）。
_CONFIG_CACHE_TTL = 30.0
_config_cache: dict[str, tuple[float, object]] = {}


def _get_config(key: str, default):
    now = time.monotonic()
    hit = _config_cache.get(key)
    if hit and now - hit[0] < _CONFIG_CACHE_TTL:
        return hit[1]
    db = SessionLocal()
    try:
        row = db.get(SystemConfig, key)
        val = row.cfg_value.get("value", default) if row else default
    finally:
        db.close()
    _config_cache[key] = (now, val)
    return val


def _case_policy(case_id: str, with_meta: bool = False) -> dict:
    """取案件渠道的生效策略档阈值（3.3.4）。开一次 DB 会话读 policy_profiles/bindings；
    无绑定/无档回退 policy_service 的 MVP 缺省兜底。graph 决策节点统一入口。

    with_meta=True 时额外附带 `policy_meta`: {profile_id, name} ——     要求挂起前把"当时生效的策略档版本"存入检查点快照；仅带 meta，不改变阈值语义。
    """
    from app.services.policy_service import DEFAULT_MVP, resolve_policy_for_case
    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            return dict(DEFAULT_MVP)
        values = resolve_policy_for_case(db, case)
        if not with_meta:
            return values
        # 附带策略档标识（无改库：profile 的 id+name 即版本标识，更新即换新的 id/name）
        profile = _resolve_profile(db, case)
        return {**values, "policy_meta": (
            {"profile_id": profile.id, "name": profile.name} if profile else None)}
    finally:
        db.close()


def _resolve_profile(db, case):
    """解析案件渠道实际命中的策略档行（绑定优先，其次 is_default）；无档返回 None。"""
    from app.domain.models import CHANNEL_DEFAULT, PolicyBinding, PolicyProfile
    ch = case.channel or CHANNEL_DEFAULT
    bind = db.query(PolicyBinding).filter_by(channel=ch).first()
    if bind is not None:
        return db.get(PolicyProfile, bind.profile_id)
    return db.query(PolicyProfile).filter_by(is_default=True).first()


def _sync_case_sentiment(case_id: str, level: str) -> None:
    """同步舆情等级到案件行（挂起时大屏即可见）。"""
    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case:
            case.sentiment_level = level
            db.commit()
    finally:
        db.close()


# Phase O：写入轨迹前脱敏——高频敏感字段打码，长文本截断，防大屏/审计外泄。
# 规则：订单号/手机号/证件号/金额等敏感值整值打码为 “***”；OCR 原文截断保留前 200 字符。
_MASK_FIELDS = {"order_id", "id_card", "phone", "bank_account", "amount"}
_SENSITIVE_RE = re.compile(r"(1[3-9]\d{9}|[3-9]\d{16}[\dXx]|[0-9]{6,}-[0-9]{6,})")


def _redact_snapshot(snapshot: dict | None) -> dict | None:
    """递归脱敏 output_snapshot/input_snapshot（不修改调用方对象）。

    整字段值打码：订单号/金额/证件/手机号等（_MASK_FIELDS）；长字符串截断。
    """
    if snapshot is None:
        return None
    if isinstance(snapshot, dict):
        out = {}
        for k, v in snapshot.items():
            if k in _MASK_FIELDS and isinstance(v, str) and v:
                out[k] = "***"
                continue
            if isinstance(v, dict):
                out[k] = _redact_snapshot(v)
            elif isinstance(v, list):
                out[k] = [_redact_snapshot(i) if isinstance(i, (dict, list)) else i for i in v]
            elif isinstance(v, str):
                val = v
                if len(val) > 200:
                    val = val[:200] + "…"
                val = _SENSITIVE_RE.sub("***", val)
                out[k] = val
            else:
                out[k] = v
        return out
    if isinstance(snapshot, list):
        return [_redact_snapshot(i) if isinstance(i, (dict, list)) else i for i in snapshot]
    return snapshot


def _elapsed_ms(t0: float) -> int:
    """把节点 perf_counter 的起始时刻换算为毫秒耗时（最小 0，不伪造计时）。"""
    return max(int((time.monotonic() - t0) * 1000), 0)


def _log_agent(case_id: str, name: str, status: str, output: dict, error: str | None = None,
               duration_ms: int = 0, step_override: int | None = None,
               trace_id: str | None = None, model: str | None = None,
               tools: list[dict] | None = None) -> None:
    """写 Agent 轨迹行（Phase O：贯穿 trace_id + Tool Trace + 真实耗时 + 脱敏）。

    - O01：trace_id 从 state 传入（回溯到申请流的案件 trace_id）；审批 resume 阶段
      由 node 层拿 state.trace_id 继续写在同一条 trace 上（不再新造 id）。
    - O02：真实 duration_ms（节点 perf_counter 计时）；DB 写失败降级——只记日志，
      不抛异常中断主流水（trace 是旁路增强，不能拖垮退款主链路）。
    - O04：tools 为 Tool Trace 结构：[{tool_name, risk_level, status, latency_ms,
      side_effect, evidence}]，冗余列 trace（JSON）承载，向后兼容历史行。
    - O06：写库前 output/input 脱敏。
    """
    try:
        db = SessionLocal()
        try:
            db.add(AgentExecutionLog(
                case_id=case_id, agent_name=name, status=status,
                output_snapshot=_redact_snapshot(output), error_message=error,
                duration_ms=duration_ms,
                step_order=step_override if step_override is not None else NODE_STEP.get(name, 99),
                trace_id=trace_id,
                trace={"tools": tools or []} if tools is not None else None,
                model=model,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 —— trace 写失败降级：绝不中断退款主流程
        logger.exception("agent trace write degraded for case=%s node=%s", case_id, name)
        # V3-6 -4：Trace 旁路写失败 -> 本地降级队列（best-effort，永不抛出）。
        try:
            from app.services.trace_degrade import degrade_trace

            degrade_trace(kind="agent_log", trace_id=trace_id, case_id=case_id,
                          payload={"agent_name": name, "status": status,
                                   "error": error})
        except Exception:  # noqa: BLE001 —— 降级入队失败也只吞掉，绝不拖垮主链路
            pass


def _set_graph_node(case_id: str, node: str) -> None:
    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case:
            case.current_graph_node = node
            db.commit()
    finally:
        db.close()


def _load_case(case_id: str) -> RefundCase | None:
    db = SessionLocal()
    try:
        return db.get(RefundCase, case_id)
    finally:
        db.close()


# ---- 节点实现 ----

def security_gate_node(state: GraphState) -> dict:
    """输入安全网关（基线 V1.0）。

    图内二次防御：无论案件从哪条入口进入（create_refund 已由路由层过一次，
    直接 ingest/重投/历史入口），到 refund 前必经此节点。判定结果写 state（进
    Checkpointer），BLOCK -> 置 security_blocked 标志 -> 后续 amount_decision
    强制 HUMAN_REVIEW 转人工，绝不自动退款；REVIEW 同转人工；PASS 放行。
    """
    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "security_gate")

    from app.services.security_gateway import gate_text

    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            return {"security_blocked": True, "gate_result": "BLOCK",
                    "gate_reason": "case not found"}
        # 复核 refund_reason（图内二次防御：路由层已脱敏但此处再核原文风险）
        text = case.refund_reason or case.order_id or ""
        res = gate_text(db, text=text, case_id=case_id,
                        source="intake", persist=True)
    finally:
        db.close()
    logger.info("security gate case=%s result=%s score=%s",
                case_id, res.gate_result, round(res.injection_score, 3))
    _log_agent(case_id, "security_gate", "SUCCESS",
               {"gate_result": res.gate_result, "score": round(res.injection_score, 3),
                "masked": res.masked_input[:200]},
               duration_ms=_elapsed_ms(t0),
               step_override=NODE_STEP["intake"] + 1,   # anchor 在 intake 后
               trace_id=state.get("trace_id"),
               tools=[{"tool_name": "security_gateway", "risk_level": "medium",
                       "status": "ok", "latency_ms": _elapsed_ms(t0),
                       "side_effect": "security_decisions_record"}])

    blocked = res.gate_result == "BLOCK"
    review = res.gate_result == "REVIEW"
    return {
        "gate_result": res.gate_result,
        "injection_score": res.injection_score,
        "security_blocked": blocked,
        "security_review": review,
        "gate_reason": ("注入风险" if blocked else
                        ("中风险待人工核" if review else "PASS")),
    }


def _tool_gate_refund_guard(state: GraphState) -> dict:
    """退款工具前置策略校验（基线 V1.0）。

    在 refund_execution 前强制二次校验：状态可退 / 决策已审批 / 核验全 MATCH /
    未重复退款。BLOCK -> 置 finish_immediately 标志 -> 图转到 finalize 直接终态
    而不执行退款。这层是不可绕过的运行时固定策略。

    注意"决策已审批"的双轨语义：
    - 人工审批路径：human_review 已把 decision 置 APPROVED（DB final_decision 仍 PENDING，
      finalize 才写终态）——读 DB final_decision 为 PENDING 但 state.decision=APPROVED，
      应视为"已审批"；
    - 自动秒退路径：amount_decision 置 AUTO_APPROVE（DB final_decision 同样 PENDING）——
      state.decision="AUTO_APPROVE" 视为"已审批"（低于风险阈值自动放行）。
    因此不能只读 DB；须结合图 state 判定可退决策。
    """
    t0 = time.monotonic()
    case_id = state["case_id"]
    db = SessionLocal()
    state_decision = state.get("decision")   # AUTO_APPROVE / APPROVED / REJECTED / ...
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            return {"finish_immediately": True, "refund_blocked": True,
                    "refund_block_reason": "case not found"}
        from app.services.tool_gate import check_refund_gate
        # 状态可退只认 DB current_status；决策用"state 已同意 或 DB 已 APPROVED"
        decision_allowed = (state_decision in ("AUTO_APPROVE", DECISION_APPROVED)
                            or case.final_decision == DECISION_APPROVED)
        reasons = []
        if not decision_allowed:
            reasons.append(f"决策未同意（graph state={state_decision}, db={case.final_decision}）")
        # 核验必须全 MATCH（防人工误批 MISMATCH 案件仍触发退款）
        for f in ("order_verification", "product_verification", "price_verification"):
            if getattr(case, f, None) != MATCH:
                reasons.append(f"{f} 非 MATCH（{getattr(case, f, None)}）")
        # 已退/占用流水号幂等
        if case.refund_status not in (None, "NONE"):
            reasons.append(f"已退（refund_status={case.refund_status}）幂等拒绝")
        # V3-3 四同时风险门禁（资金影响工具：策略+HITL+幂等+审计 全要）。
        # 纯读校验（persist=False 不额外落库）；审计由本守卫既有路径承诺
        # （BLOCK 落 security_decisions；PASS 由 refund_gate 成功轨迹留痕）。
        from app.services.security_gateway import check_tool_risk_gate
        risk_dec = check_tool_risk_gate(
            db, tool_name="execute_refund", case=case,
            hitl_approved=decision_allowed,
            persist=False, audit_committed=True)
        if not risk_dec.allow:
            for _dim, _why in risk_dec.missing:
                reasons.append(f"风险门禁[{_dim}]: {_why}")
        # V3-3 Fail-Closed：安全服务（输入网关）故障时，资金面高风险操作直接拒绝，
        # 绝不因安全服务不可用而放开退款（默认健康 -> ALLOW，零行为变化）。
        from app.services.fail_closed import SVC_SECURITY_GATEWAY, guard_high_risk
        fc_dec = guard_high_risk(db, service=SVC_SECURITY_GATEWAY, persist=False)
        if fc_dec.action != "ALLOW":
            reasons.append(f"Fail-Closed[{fc_dec.service}]: {fc_dec.reason}")
        allow = not reasons
        if not allow:
            # 落 security_decisions 审计（tool_gate BLOCK）
            from app.domain.models import SecurityDecision, new_uuid
            db.add(SecurityDecision(
                id=new_uuid(), case_id=case_id, source="refund_tool",
                gate_result="BLOCK", injection_score=0.0, risk_level="MEDIUM",
                masked_input="; ".join(reasons)[:500],
                tool_name="execute_refund", rule_hits={"tool_gate_reasons": reasons},
            ))
            db.commit()
    finally:
        db.close()

    _log_agent(case_id, "refund_gate", "SUCCESS",
               {"allow": allow, "reason": reasons},
               duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"),
               tools=[{"tool_name": "refund_tool_gate", "risk_level": "high",
                       "status": "blocked" if not allow else "ok",
                       "latency_ms": _elapsed_ms(t0),
                       "side_effect": "security_decisions_refund_tool"}])
    return {
        "finish_immediately": not allow,
        "refund_blocked": not allow,
        "refund_block_reason": "; ".join(reasons) if not allow else "",
    }


def intake_node(state: GraphState) -> dict:
    t0 = time.monotonic()
    case = _load_case(state["case_id"])
    if case is None:
        raise ValueError(f"case not found: {state['case_id']}")
    trace_id = state.get("trace_id", case.id)
    _log_agent(case.id, "intake", "SUCCESS",
               {"order_id": case.order_id, "amount": str(case.requested_amount)},
               duration_ms=_elapsed_ms(t0), trace_id=trace_id,
               tools=[{"tool_name": "load_case", "risk_level": "low", "status": "ok",
                       "latency_ms": _elapsed_ms(t0),
                       "side_effect": "case_row_read"}])
    _set_graph_node(case.id, "intake")
    return {
        "case_id": case.id,
        "amount_cents": int(case.requested_amount * 100),
        "refund_reason": case.refund_reason,
        "trace_id": state.get("trace_id", case.id),
    }


def verification_stage_node(state: GraphState) -> dict:
    """订单真实性 / 商品一致性 / 价格一致性。

    三个核验共享订单源，串行执行（不用 asyncio：主图本身串行、顺序副作用在退款审批链路
    上必须保持）。任一核验 MISMATCH / UNKNOWN -> 后续 amount_decision 强制挂起转人工。
    核验结果写 state（进 Checkpointer）+ 更新 RefundCase 行 + 写 AgentExecutionLog。
    """
    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "verification")

    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            raise ValueError(f"case not found: {case_id}")
        submitter = db.get(User, case.submitter_id)
        requester = submitter.username if submitter else ""
        order_id = case.order_id
        amount_yuan = float(case.requested_amount)
        reason = case.refund_reason
    finally:
        db.close()

    outcome = run_verification(
        order_id=order_id,
        requester_username=requester,
        requested_amount_yuan=amount_yuan,
        refund_reason=reason,
        provider=get_order_provider(),
    )

    # 写回案件行（大屏/详情可读；口径与 state 一致）
    db2 = SessionLocal()
    try:
        case2 = db2.get(RefundCase, case_id)
        if case2 is not None:
            case2.order_verification = outcome.order_verification
            case2.order_verification_reasons = outcome.order_reasons
            case2.product_verification = outcome.product_verification
            case2.product_verification_reasons = outcome.product_reasons
            case2.price_verification = outcome.price_verification
            case2.price_verification_reasons = outcome.price_reasons
            db2.commit()
    finally:
        db2.close()

    _log_agent(case_id, "verification", "SUCCESS",
               {
                   "order": {"verdict": outcome.order_verification, "reasons": outcome.order_reasons},
                   "product": {"verdict": outcome.product_verification, "reasons": outcome.product_reasons},
                   "price": {"verdict": outcome.price_verification, "reasons": outcome.price_reasons},
                   "node": NODE_STEP["verification"],
               },
               duration_ms=_elapsed_ms(t0), step_override=NODE_STEP["verification"],
               trace_id=state.get("trace_id"),
               tools=[{"tool_name": "order_verification", "risk_level": "low",
                       "status": "ok", "latency_ms": _elapsed_ms(t0),
                       "side_effect": "case_row_verification_columns"}])
    return {
        "order_verification": outcome.order_verification,
        "order_verify_reasons": outcome.order_reasons,
        "product_verification": outcome.product_verification,
        "product_verify_reasons": outcome.product_reasons,
        "price_verification": outcome.price_verification,
        "price_verify_reasons": outcome.price_reasons,
    }


def ocr_node(state: GraphState) -> dict:
    """多附件多模态节点：逐张 OCR/VL，再做整份申请的交叉一致性裁决。"""
    from app.infrastructure.ocr import OcrResult, get_ocr_provider
    from app.infrastructure.vision import VisionProviderError, get_vision_provider
    from app.services.multimodal_evidence import (
        aggregate_evidence_consistency,
        fuse_multimodal_evidence,
    )
    from app.services.ocr_order_match import cross_check_ocr_fields

    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "ocr")
    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        evidence_rows = (
            db.query(Evidence).filter_by(case_id=case_id)
            .order_by(Evidence.created_at, Evidence.id).all()
        )
        if case is None:
            raise ValueError(f"case not found: {case_id}")
    finally:
        db.close()
    if not evidence_rows:
        _log_agent(case_id, "ocr", "FAILED", {}, error="无凭证文件",
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        return {
            "ocr_status": OCR_STATUS_FAILED, "evidence_status": "FAILED",
            "ocr_confidence": 0.0, "ocr_text": "", "visual_evidence": {},
        }

    trusted_items = []
    try:
        lookup = get_order_provider().lookup(case.order_id)
        if lookup.ok and lookup.order is not None:
            trusted_items = lookup.order.items
    except Exception:
        trusted_items = []

    application = {
        "order_id": case.order_id,
        "order_no": case.order_no,
        "requested_amount": str(case.requested_amount),
        "reason_codes": list(case.reason_codes or []),
        "refund_reason": case.refund_reason or state.get("refund_reason") or "",
        "product_skus": [item.sku for item in trusted_items if getattr(item, "sku", None)],
        "product_names": [item.name for item in trusted_items if getattr(item, "name", None)],
    }
    claim_context = (
        f"售后类型：{'、'.join(application['reason_codes']) or '未填写'}；"
        f"订单号：{application['order_id']}；申请金额：{application['requested_amount']}；"
        f"订单商品：{'、'.join(application['product_names']) or '未知'}；"
        f"商品SKU：{'、'.join(application['product_skus']) or '未知'}；"
        f"用户说明：{application['refund_reason']}"
    )[:2000]

    threshold = _case_policy(case_id).get("ocr_confidence", settings.OCR_CONFIDENCE_THRESHOLD)
    if threshold is not None and threshold > 1:
        threshold = threshold / 100.0
    ocr_provider = get_ocr_provider()
    vision_provider = get_vision_provider()
    analyzed: list[dict] = []
    per_row_status: dict[str, str] = {}
    errors: list[str] = []
    ocr_latency = 0
    vision_latency = 0
    model_used = False
    combined_cross = {"updates": {}, "reasons": {"order": [], "product": [], "price": []}}

    def merge_verdict(field: str, incoming: str) -> None:
        rank = {"UNKNOWN": 0, "MATCH": 1, "MISMATCH": 2}
        current = combined_cross["updates"].get(field, "UNKNOWN")
        if rank.get(incoming, 0) >= rank.get(current, 0):
            combined_cross["updates"][field] = incoming

    for ev in evidence_rows:
        ocr_started = time.monotonic()
        ocr_error = None
        try:
            result = ocr_provider.ocr(ev.file_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr failed for evidence %s: %s", ev.id, exc)
            ocr_error = str(exc)[:300]
            result = OcrResult(text="", fields={}, confidence=0.0)
        ocr_latency += _elapsed_ms(ocr_started)
        status = (OCR_STATUS_SUCCESS if not ocr_error and result.confidence >= threshold
                  else OCR_STATUS_UNRELIABLE if not ocr_error else OCR_STATUS_FAILED)
        raw_fields = dict(result.fields or {})
        normalized_text = re.sub(r"\s+", "", result.text or "").upper()
        structured_voucher = "电子订单凭证" in normalized_text or "EGETEXPRESS" in normalized_text
        if structured_voucher and not {"order_id", "amount"}.issubset(raw_fields):
            status = OCR_STATUS_UNRELIABLE

        vision_result = None
        vision_error = None
        vision_started = time.monotonic()
        try:
            vision_result = vision_provider.analyze(ev.file_url, claim_context)
            model_used = True
        except VisionProviderError as exc:
            vision_error = exc.code
            logger.warning("vision evidence %s degraded (%s): %s", ev.id, exc.code, exc)
        except Exception as exc:  # noqa: BLE001
            vision_error = "FAILED"
            logger.exception("vision evidence %s failed: %s", ev.id, exc)
        vision_latency += _elapsed_ms(vision_started)
        if ocr_error or vision_error:
            errors.append(f"{ev.id}: {ocr_error or vision_error}")

        item_summary = fuse_multimodal_evidence(
            ocr_result=result, ocr_status=status, vision_result=vision_result,
            vision_error=vision_error, min_vision_confidence=settings.VISION_MIN_CONFIDENCE,
        )
        item_summary["structured_voucher"] = structured_voucher
        file_name = Path(ev.file_url).name.split("__", 2)[-1]
        analyzed.append({"evidence_id": ev.id, "file_name": file_name, "summary": item_summary})
        per_row_status[ev.id] = status

        checked = cross_check_ocr_fields(
            expected_order_id=case.order_id,
            expected_amount=case.requested_amount,
            trusted_items=trusted_items,
            fields=item_summary.get("fields") or {},
        )
        for field, verdict in checked["updates"].items():
            merge_verdict(field, verdict)
        for reason_key in ("order", "product", "price"):
            combined_cross["reasons"][reason_key].extend(checked["reasons"].get(reason_key) or [])

    summary = aggregate_evidence_consistency(application=application, evidences=analyzed)
    fields = dict(summary.get("fields") or {})
    overall_ocr_status = (
        OCR_STATUS_SUCCESS if summary["evidence_status"] in {"SUCCESS", "DEGRADED"}
        else OCR_STATUS_FAILED if all(v == OCR_STATUS_FAILED for v in per_row_status.values())
        else OCR_STATUS_UNRELIABLE
    )
    state_updates = {
        "ocr_text": summary.get("text") or "", "ocr_fields": fields,
        "ocr_confidence": summary.get("overall_confidence", 0.0), "ocr_status": overall_ocr_status,
        "evidence_status": summary["evidence_status"], "visual_evidence": summary,
    }

    db2 = SessionLocal()
    try:
        case = db2.get(RefundCase, case_id)
        if case:
            reason_attrs = {
                "order_verification": ("order_verification_reasons", "order", "order_verify_reasons"),
                "product_verification": ("product_verification_reasons", "product", "product_verify_reasons"),
                "price_verification": ("price_verification_reasons", "price", "price_verify_reasons"),
            }
            for verdict_field, verdict in combined_cross["updates"].items():
                setattr(case, verdict_field, verdict)
                db_reason_attr, reason_key, state_reason_key = reason_attrs[verdict_field]
                merged = list(getattr(case, db_reason_attr) or [])
                merged.extend(combined_cross["reasons"].get(reason_key) or [])
                setattr(case, db_reason_attr, list(dict.fromkeys(merged)))
                state_updates[verdict_field] = verdict
                state_updates[state_reason_key] = getattr(case, db_reason_attr)
            case.ocr_summary = summary
        for item in analyzed:
            evidence_row = db2.get(Evidence, item["evidence_id"])
            if evidence_row is not None:
                evidence_row.ocr_result = item["summary"]
                evidence_row.ocr_status = per_row_status[item["evidence_id"]]
        db2.commit()
    finally:
        db2.close()

    log_status = "SUCCESS" if summary["evidence_status"] in ("SUCCESS", "DEGRADED") else summary["evidence_status"]
    _log_agent(
        case_id, "ocr", log_status,
        {
            "evidence_status": summary["evidence_status"],
            "confidence": summary["overall_confidence"],
            "fields": fields,
            "claim_alignment": summary["claim_alignment"],
            "field_conflicts": summary["field_conflicts"],
            "consistency": summary["consistency"],
            "attachment_count": len(analyzed),
            "cross_check": combined_cross,
        },
        error="; ".join(errors)[:500] or None,
        duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"),
        model=settings.VISION_MODEL if model_used else None,
        tools=[
            {"tool_name": "ocr_extract", "risk_level": "medium",
             "status": "ok" if overall_ocr_status == OCR_STATUS_SUCCESS else "degraded",
             "latency_ms": ocr_latency, "side_effect": "structured_text_extract"},
            {"tool_name": "qwen_vl_analyze", "risk_level": "medium",
             "status": "ok" if model_used else "failed",
             "latency_ms": vision_latency, "side_effect": "visual_description_only"},
            {"tool_name": "evidence_fusion", "risk_level": "medium",
             "status": summary["evidence_status"].lower(), "latency_ms": 0,
             "side_effect": "evidence_summary_write"},
        ],
    )
    return state_updates


def fraud_node(state: GraphState) -> dict:
    """规则组合评分（裁决 D4）。差异说明见 Phase 5 报告：
    数据模型无手机号/IP，高频与退款率规则以 order_id 重复申请近似实现。
    3.3.4：阈值按渠道生效策略档解析，取代旧 _get_config。
    """
    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "fraud")
    pol = _case_policy(case_id)
    threshold = pol["fraud_suspend"]
    auto_pass = pol["fraud_pass"]
    amount_threshold = float(pol["amount_threshold"])

    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            raise ValueError(f"case not found: {case_id}")
        score, rules = 0, []

        # 多模态凭证与用户诉求、可信订单的矛盾进入风控分。
        # UNKNOWN/证据不足不计欺诈分，避免把“看不清”误判为“造假”。
        from app.services.multimodal_evidence import score_evidence_risk

        evidence_score, evidence_rules = score_evidence_risk(
            summary=state.get("visual_evidence"),
            order_verification=state.get("order_verification"),
            product_verification=state.get("product_verification"),
            price_verification=state.get("price_verification"),
        )
        score += evidence_score
        rules.extend(evidence_rules)

        # 规则1：凭证复用（MD5/pHash 出现在其他案件）+30
        for ev in db.query(Evidence).filter_by(case_id=case_id).all():
            q = db.query(Evidence).filter(
                Evidence.file_md5 == ev.file_md5,
                Evidence.file_md5.isnot(None),
                Evidence.case_id != case_id,
            ).first()
            if q is not None:
                score += 30
                rules.append("evidence_reuse")
                break

        # 规则2：同 order_id 24h 内重复申请 >= 3 次 +25（近似手机号/IP 高频规则）
        from datetime import datetime, timedelta
        since = datetime.now() - timedelta(hours=24)
        cnt = db.query(RefundCase).filter(
            RefundCase.order_id == case.order_id,
            RefundCase.created_at >= since,
            RefundCase.id != case_id,
        ).count()
        if cnt >= 3:
            score += 25
            rules.append("high_frequency")

        # 规则3：同 order_id 历史多次申请（退款率近似）>= 2 次 +20
        hist = db.query(RefundCase).filter(
            RefundCase.order_id == case.order_id, RefundCase.id != case_id
        ).count()
        if hist >= 2:
            score += 20
            rules.append("high_refund_rate")

        # 规则4：金额恰低于阈值（卡阈值） +15
        amt = float(case.requested_amount)
        if 0 < amount_threshold - amt <= 1.00:
            score += 15
            rules.append("threshold_carding")

        score = min(100, score)
        rules = list(dict.fromkeys(rules))
        _log_agent(case_id, "fraud", "SUCCESS", {"score": score, "rules": rules,
                                             "node": NODE_STEP["fraud"]},
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        case.fraud_score = score  # 同步案件行：挂起时主管即可看到风险分
        db.commit()
        return {"fraud_score": score, "fraud_rules": rules}
    finally:
        db.close()


def sentiment_node(state: GraphState) -> dict:
    from app.infrastructure.llm import get_sentiment_provider
    from app.services.multimodal_evidence import build_sentiment_context

    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "sentiment")
    # 舆情主输入是用户退款说明；只附加脱敏后的客观视觉事实，订单字段、手机号、
    # 图片指令和 suspicious_signals 不进入云端 LLM。
    text = build_sentiment_context(
        state.get("refund_reason"), state.get("visual_evidence")
    )
    try:
        provider = get_sentiment_provider()
        result = provider.analyze(text)  # SentimentResult(sentiment_level, keywords)
        _log_agent(case_id, "sentiment", "SUCCESS",
                   {"level": result.sentiment_level, "keywords": result.keywords},
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        _sync_case_sentiment(case_id, result.sentiment_level)
        return {"sentiment_level": result.sentiment_level, "sentiment_keywords": result.keywords}
    except Exception as exc:  # noqa: BLE001 —— LLM 不可用 -> 保守 HIGH（转人工）
        logger.warning("sentiment unavailable, conservative HIGH: %s", exc)
        _log_agent(case_id, "sentiment", "FAILED", {}, error=str(exc)[:500],
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        _sync_case_sentiment(case_id, "HIGH")
        return {"sentiment_level": "HIGH", "sentiment_keywords": []}


def amount_decision_node(state: GraphState) -> dict:
    """确定性规则层（docs/06 ，不用 LLM 决策金额）。"""
    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "amount_decision")

    pol = _case_policy(case_id, with_meta=True) # ：附带策略档 meta 快照
    amount_threshold = pol.get("amount_threshold", _get_config("refund.amount_threshold", 300))
    suspend_threshold = pol.get("fraud_suspend", _get_config("fraud.suspend_threshold", 60))
    auto_pass_threshold = pol.get("fraud_pass", _get_config("fraud.auto_pass_threshold", 20))

    amount_yuan = Decimal(state["amount_cents"]) / 100
    fraud = state.get("fraud_score", 0)
    sentiment = state.get("sentiment_level", "LOW")
    ocr_status = state.get("ocr_status", OCR_STATUS_SUCCESS)
    evidence_status = state.get("evidence_status", ocr_status)

    # 订单/商品/价格核验：任一 MISMATCH/UNKNOWN -> 强制挂起转人工（不自动退款）
    verifications = [
        (state.get("order_verification"), "订单真实性"),
        (state.get("product_verification"), "商品一致性"),
        (state.get("price_verification"), "价格一致性"),
    ]
    non_match = [name for verdict, name in verifications
                 if verdict in (MISMATCH, "UNKNOWN")]

    # 输入安全网关：security_gate 节点若判定 BLOCK/REVIEW ->
    # 强制人工，绝不自动退款（：BLOCK 不回写 case 语义，直接人工接管）。
    security_blocked = bool(state.get("security_blocked"))
    security_review = bool(state.get("security_review"))

    if security_blocked or security_review:
        decision = "HUMAN_REVIEW"
        reason = f"输入安全网关 {state.get('gate_result')}（{state.get('gate_reason')}）强制人工复核"
    elif non_match:
        decision = "HUMAN_REVIEW"
        reason = f"核验未通过: {', '.join(non_match)}"
    elif evidence_status in ("UNRELIABLE", "FAILED", "UNSAFE"):
        decision = "HUMAN_REVIEW"
        reason = f"多模态凭证状态 {evidence_status}，强制人工复核"
    elif fraud >= suspend_threshold or sentiment == "HIGH":
        decision = "HUMAN_REVIEW"
        reason = f"欺诈分 {fraud} 或舆情 {sentiment} 高风险"
    elif amount_yuan > amount_threshold:
        decision = "HUMAN_REVIEW"
        reason = f"金额 {amount_yuan} 超阈值 {amount_threshold}"
    elif fraud <= auto_pass_threshold and amount_yuan <= amount_threshold:
        decision = "AUTO_APPROVE"
        reason = f"欺诈分 {fraud} 低风险，金额未超阈值"
    else:
        decision = "HUMAN_REVIEW"
        reason = f"欺诈分 {fraud} 处于保守区间（>{auto_pass_threshold}），转人工"

    _log_agent(case_id, "amount_decision", "SUCCESS",
               {"decision": decision, "reason": reason, "node": NODE_STEP["amount_decision"]},
               duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"),
               model=(pol.get("policy_meta") or {}).get("name") or None,
               tools=[{"tool_name": "case_policy", "risk_level": "medium",
                       "status": "ok", "latency_ms": _elapsed_ms(t0),
                       "side_effect": "decision_matrix_apply"}])
    # ：挂起时把独立挂起原因 + 策略档 meta + 待审批信息写入 state（进 Checkpointer）。
    # pending_approval 必须在此处（决策节点）构建：human_review_node 的 interrupt()
    # 在函数体内首个求值处挂起，之后的所有语句（含它的 return）都不执行，
    # 若等它构建，挂起 checkpoint 里将缺失该字段。
    # 非挂起决策时 suspend_reason/pending_approval 保持为空，避免污染 AUTO_APPROVE 用例。
    pending = None
    if decision == "HUMAN_REVIEW":
        pending = {
            "required_role": "supervisor",
            "action": "approve|reject",
            "note": reason,
        }
    return {
        "decision": decision,
        "review_reason": reason,
        "suspend_reason": reason if decision == "HUMAN_REVIEW" else None,
        "policy_version": pol.get("policy_meta"),
        "pending_approval": pending,
    }


def human_review_node(state: GraphState) -> dict:
    """人工断点：interrupt 挂起；resume 后接收 {action, opinion}。"""
    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "human_review")
    # interrupt() 首次执行（无外部 resume 输入）时返回 __interrupt__ 并挂起。
    # 待审批信息已由 amount_decision_node 写入 state（进 Checkpointer）——interrupt
    # 在函数体首个求值处挂起，之后语句不执行，故这里直接用 state 的 pending_approval。
    resume = interrupt({"node": "human_review", "case_id": case_id,
                        "reason": state.get("review_reason", ""),
                        "pending_approval": state.get("pending_approval")})
    action = resume.get("action", "REJECTED")
    opinion = resume.get("opinion", "")
    # Phase O（O03）：human_review 的 trace 必须延续原 case 的 trace_id——审批 resume
    # 阶段走的是同一个图实例（thread_id=case_id），不能新造 trace_id 割裂贯穿轨迹。
    _log_agent(case_id, "human_review", "SUCCESS",
               {"action": action, "opinion": opinion, "node": NODE_STEP["human_review"]},
               duration_ms=_elapsed_ms(t0),
               trace_id=state.get("trace_id"),
               tools=[{"tool_name": "human_intervention", "risk_level": "high",
                       "status": "ok", "latency_ms": 0,
                       "side_effect": "human_interventions_record"}])
    return {
        "decision": "APPROVED" if action == "APPROVED" else "REJECTED",
        "review_reason": opinion,
    }


def refund_execution_node(state: GraphState) -> dict:
    """执行退款（MockRefundProvider，Phase 7 强化幂等与流水号）。

    执行前强制过 tool_gate：状态可退/决策已审批/核验全 MATCH/
    未重复退款。BLOCK -> 不执行退款，直接转 SYSTEM_FAILURE 语义终态（保留人工接管
    记录）。这层不可绕过：任何进入 refund_execution 的路径都先过守卫。
    """
    from app.infrastructure.refund import MockRefundProvider

    t0 = time.monotonic()
    case_id = state["case_id"]
    _set_graph_node(case_id, "refund_execution")

    # 二道防线：tool_gate 前置策略校验（不可绕过）
    guard = _tool_gate_refund_guard(state)
    if guard.get("refund_blocked"):
        _log_agent(case_id, "refund_execution", "FAILED", {},
                   error=f"tool_gate BLOCK: {guard.get('refund_block_reason')}",
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        return {"decision": DECISION_SYSTEM_FAILURE,
                "review_reason": f"退款前置策略校验未通过: {guard.get('refund_block_reason')}"}

    provider = MockRefundProvider()
    ok, err = provider.execute(case_id, Decimal(state["amount_cents"]) / 100)
    if not ok:
        _log_agent(case_id, "refund_execution", "FAILED", {}, error=err,
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        return {"decision": DECISION_SYSTEM_FAILURE, "review_reason": f"退款执行失败: {err}"}
    _log_agent(case_id, "refund_execution", "SUCCESS",
               {"amount": str(Decimal(state["amount_cents"]) / 100),
                "refund_id": provider.last_refund_id},
               duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"),
               tools=[{"tool_name": "refund_provider", "risk_level": "high",
                       "status": "ok", "latency_ms": _elapsed_ms(t0),
                       "side_effect": "refund_executed"}]),
    return {"decision": DECISION_APPROVED}


def finalize_node(state: GraphState) -> dict:
    """按 state.decision 写库：状态机校验 + COMPLETED + 决策终态。"""
    t0 = time.monotonic()
    case_id = state["case_id"]
    decision = state["decision"]
    _set_graph_node(case_id, "finalize")
    db = SessionLocal()
    try:
        case = db.get(RefundCase, case_id)
        if case is None:
            return {}
        if case.final_decision != DECISION_PENDING:
            return {}  # 幂等：已终态不重写
        new_decision = decision if decision in (
            DECISION_APPROVED, DECISION_REJECTED, DECISION_SYSTEM_FAILURE) else DECISION_APPROVED
        ns, nd = transition_case(
            current_status=case.current_status,
            current_decision=case.final_decision,
            new_status=CASE_STATUS_COMPLETED,
            new_decision=new_decision,
        )
        case.current_status = ns
        case.final_decision = nd
        if case.fraud_score is None and "fraud_score" in state:
            case.fraud_score = state["fraud_score"]
        if case.sentiment_level is None and "sentiment_level" in state:
            case.sentiment_level = state["sentiment_level"]
        case.ocr_summary = state.get("visual_evidence") or {
            "text": state.get("ocr_text", ""),
            "fields": state.get("ocr_fields", {}),
            "confidence": state.get("ocr_confidence", 0.0),
        }
        db.commit()
        logger.info("case %s finalized: %s / %s", case_id, ns, nd)
        _log_agent(case_id, "finalize", "SUCCESS", {"status": ns, "decision": nd},
                   duration_ms=_elapsed_ms(t0), trace_id=state.get("trace_id"))
        return {}
    finally:
        db.close()


# ---- 图构建 ----
def build_graph():
    g = StateGraph(GraphState)
    g.add_node("security_gate", security_gate_node)
    g.add_node("intake", intake_node)
    g.add_node("verification", verification_stage_node)
    g.add_node("ocr", ocr_node)
    g.add_node("fraud", fraud_node)
    g.add_node("sentiment", sentiment_node)
    g.add_node("amount_decision", amount_decision_node)
    g.add_node("human_review", human_review_node)
    g.add_node("refund_execution", refund_execution_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "security_gate")
    g.add_edge("security_gate", "intake")
    g.add_edge("intake", "verification")   # 订单/商品/价格核验先于业务决策
    g.add_edge("verification", "ocr")
    # 凭证异常也必须经过风控落分与舆情安全上下文，再由确定性决策节点转人工；
    # 这样主管能看到完整风险原因，而不是从 OCR 节点直接跳过后续 Agent。
    g.add_edge("ocr", "fraud")
    g.add_edge("fraud", "sentiment")
    g.add_edge("sentiment", "amount_decision")
    g.add_conditional_edges(
        "amount_decision",
        lambda s: "refund_execution" if s.get("decision") == "AUTO_APPROVE" else "human_review",
        {"refund_execution": "refund_execution", "human_review": "human_review"},
    )
    g.add_conditional_edges(
        "human_review",
        lambda s: "refund_execution" if s.get("decision") == "APPROVED" else "finalize",
        {"refund_execution": "refund_execution", "finalize": "finalize"},
    )
    g.add_edge("refund_execution", "finalize")
    g.add_edge("finalize", END)
    return g
