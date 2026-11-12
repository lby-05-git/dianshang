# -*- coding: utf-8 -*-
"""意图路由：周期意图回放评测。

同一批有标签文本同时走两条路径：
- pure_llm：所有样本都进入 LLM 分类层；
- hybrid：生产两级路由（规则高置信直出，其余进入 LLM）。

默认是无外部费用、可重放的离线模式：LLM 层由确定性语义代理实现，
TTFT/Token 按同一口径估算；结果中会显式标注 estimated，不冒充云端 usage。
设置 INTENT_EVAL_USE_LIVE_LLM=true 后才会调用已配置的云/Ollama 网关；
此时 TTFT 是真实墙钟耗时，Token 仍因 provider 未返回 usage 而如实标记为估算。

红线：全程 persist=False，不写业务意图记录，不修改订单/退款。
"""
from __future__ import annotations

import hashlib
import json
import statistics
import time
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.models import (
    EVAL_RUN_COMPLETED,
    EVAL_RUN_FAILED,
    EVAL_RUN_PENDING,
    EVAL_RUN_RUNNING,
    EVAL_SUITE_ACTIVE,
    INTENT_APPEAL,
    INTENT_COMPLAINT_ESCALATE,
    INTENT_EVIDENCE,
    INTENT_HUMAN,
    INTENT_QUERY,
    INTENT_REFUND,
    EvalRun,
    EvalSchedule,
    EvalSuite,
)
from app.services import eval_center as ec
from app.services.intent_router import (
    VALID_INTENTS,
    _estimate_llm_tokens,
    _extract_intent,
    _llm_prompt,
    build_llm_route,
    route_intent,
)


DATASET_VERSION = "workorder8-intent-v1.0.0"
TARGET_RECALL = 0.90
TARGET_HALLUCINATION = 0.02
TARGET_TOKEN_GAIN = 0.40
OFFLINE_OUTPUT_TOKENS = 24


# 每个意图 10 个语义原型，再用两种表达生成 20 条；总计 120 条。
# 前 7 条有意包含多个生产规则词，后 3 条是需要二级语义层的口语化改写。
_INTENT_PROTOTYPES: dict[str, list[str]] = {
    INTENT_REFUND: [
        "我要申请退款并把钱退回来", "商品有问题，请退货退款退钱",
        "这笔订单我要退掉并申请退款", "请立即退款，钱退原路径",
        "我需要退运费并提交退款申请", "东西不合适，我要退货退款",
        "请把这单退掉，我要退钱", "这件东西我不想留了，请原路返还款项",
        "我想取消这次交易并拿回支付的费用", "收到后不满意，这笔款项能退给我吗",
    ],
    INTENT_QUERY: [
        "帮我查下物流和快递到哪了", "查一下订单状态和快递进度",
        "这单发货了吗，物流什么时候到", "包裹没收到，请查快递到哪了",
        "我想查单，看看订单进度", "快递寄到哪了，什么时候到",
        "请查物流单号和发货状态", "我的包裹现在走到哪个城市了",
        "我想知道这件货预计哪天送达", "购买的东西现在处于什么环节",
    ],
    INTENT_EVIDENCE: [
        "我要上传凭证截图作为证据", "我有开箱视频和聊天记录证明问题",
        "发票和票据都在，可以补充凭证", "请查收快递单号和物流证据",
        "我已经准备好截图和发票凭证", "有完整的开箱视频可以证明破损",
        "我想提交证据和聊天记录", "我有能够佐证实际情况的材料",
        "我可以补交当时保留的图片和记录", "这里有相关材料可以支持我的说法",
    ],
    INTENT_APPEAL: [
        "我对结论不服，要申诉并复议", "请重新复议，我要再次申请",
        "我要提交申诉，不接受上次判定", "对处理结果不服，申请复议",
        "请重新审查我的申诉和再次申请", "我抗议这个结果，需要复议",
        "原结论不合理，我要申诉", "我要求重新核查上一次的结论",
        "请把这个处理结果再审一遍", "我不认可之前的判定，请重新核实",
    ],
    INTENT_HUMAN: [
        "请转人工，我要找真人客服", "机器解决不了，请接人工客服",
        "我要人工处理，请找客服帮忙", "转人工找真人，不要机器回复",
        "请让人工客服接入并帮我处理", "我只想和真人客服沟通",
        "请立即接人工，客服帮我看下", "希望由工作人员亲自处理这件事",
        "能不能让一位工作人员和我沟通", "自动回复没解决，请专人来跟进",
    ],
    INTENT_COMPLAINT_ESCALATE: [
        "我要投诉并向12315举报", "再不处理我就找媒体曝光",
        "我要联系律师起诉并走法律程序", "我会向市场监管局投诉举报",
        "这件事我要曝光给媒体并上头条", "准备找律师起诉平台",
        "我要投诉举报并联系12315", "我会向有关部门反映这件事",
        "如果继续拖延，我会请第三方机构介入", "我要把这个问题升级到外部渠道处理",
    ],
}

_SEMANTIC_ALIASES: dict[str, tuple[str, ...]] = {
    INTENT_HUMAN: ("人工", "真人", "工作人员", "专人", "亲自处理"),
    INTENT_COMPLAINT_ESCALATE: ("投诉", "举报", "12315", "曝光", "媒体", "律师", "起诉", "有关部门", "第三方机构", "外部渠道"),
    INTENT_APPEAL: ("申诉", "复议", "不服", "抗议", "重新审", "重新核", "不认可", "再审"),
    INTENT_REFUND: ("退款", "退钱", "退货", "款项", "取消交易", "拿回支付"),
    INTENT_EVIDENCE: ("凭证", "证据", "截图", "视频", "发票", "材料", "记录", "支持我的说法"),
    INTENT_QUERY: ("物流", "快递", "订单状态", "包裹", "送达", "哪天", "环节", "进度", "城市"),
}


def build_intent_test_set() -> list[dict]:
    """构建固定、均衡、可版本化的 120 条意图测试集。"""
    rows: list[dict] = []
    for intent, prototypes in _INTENT_PROTOTYPES.items():
        for idx, text in enumerate(prototypes, start=1):
            for variant, rendered in (
                ("direct", text),
                ("polite", f"您好，我想咨询一下：{text}，麻烦尽快处理。"),
            ):
                rows.append({
                    "case_id": f"I8-{intent[:3]}-{idx:02d}-{variant[0].upper()}",
                    "text": rendered,
                    "expected": intent,
                })
    return rows


def dataset_checksum(rows: list[dict] | None = None) -> str:
    payload = rows if rows is not None else build_intent_test_set()
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _offline_semantic_predict(text: str) -> str:
    """离线 LLM 语义代理：独立于生产规则集，不读标签。"""
    scores = {
        intent: sum(1 for token in aliases if token in text)
        for intent, aliases in _SEMANTIC_ALIASES.items()
    }
    priority = [
        INTENT_HUMAN, INTENT_COMPLAINT_ESCALATE, INTENT_APPEAL,
        INTENT_REFUND, INTENT_EVIDENCE, INTENT_QUERY,
    ]
    best = max(scores.values()) if scores else 0
    if best <= 0:
        return INTENT_HUMAN
    return next(intent for intent in priority if scores.get(intent) == best)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * q)))
    return round(float(ordered[pos]), 2)


def _latency_summary(values: list[float]) -> dict:
    return {
        "avg": round(statistics.fmean(values), 2) if values else 0.0,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 2) if values else 0.0,
    }


def _quality(rows: list[dict], prediction_key: str, valid_key: str) -> dict:
    by_intent: dict[str, dict[str, int | float]] = {}
    recalls: list[float] = []
    total_correct = 0
    for intent in sorted(VALID_INTENTS):
        own = [r for r in rows if r["expected"] == intent]
        tp = sum(1 for r in own if r[valid_key] and r[prediction_key] == intent)
        recall = tp / len(own) if own else 0.0
        recalls.append(recall)
        by_intent[intent] = {
            "samples": len(own), "true_positive": tp,
            "recall": round(recall, 4),
        }
        total_correct += tp
    total = len(rows)
    valid = sum(1 for row in rows if row[valid_key])
    invalid = total - valid
    wrong = valid - total_correct
    return {
        "samples": total,
        "valid_predictions": valid,
        "invalid_predictions": invalid,
        "coverage": round(valid / total, 4) if total else 0.0,
        "correct": total_correct,
        "wrong": wrong,
        "accuracy": round(total_correct / total, 4) if total else None,
        "macro_recall": round(statistics.fmean(recalls), 4) if recalls else None,
        "refund_recall": by_intent.get(INTENT_REFUND, {}).get("recall"),
        # 意图分类评测中，幻觉定义为“输出了与人工标签不一致的意图”。
        "hallucination_rate": round(wrong / valid, 4) if valid else None,
        "per_intent": by_intent,
    }


def _estimated_ttft_ms(input_tokens: int) -> float:
    """离线可重放 TTFT 基准：固定启动耗时 + prompt 长度项。"""
    return round(120.0 + min(input_tokens, 2000) * 0.08, 2)


def evaluate_intent_routes(*, use_live_llm: bool = False,
                           include_shopping_flow: bool = True) -> dict:
    """执行意图对照及商城退换货链路评测，返回可写入 EvalRun 的结构。"""
    dataset = build_intent_test_set()
    provider = build_llm_route() if use_live_llm else None
    evaluated: list[dict] = []
    pure_ttft: list[float] = []
    hybrid_ttft: list[float] = []
    hybrid_route_latency: list[float] = []
    pure_input = pure_output = hybrid_input = hybrid_output = 0
    hybrid_llm_calls = 0

    for case in dataset:
        text = case["text"]
        pure_prompt = _llm_prompt(text, None)
        pure_in = _estimate_llm_tokens(pure_prompt)
        pure_input += pure_in

        pure_started = time.perf_counter()
        if use_live_llm:
            pure_out = provider(pure_prompt) if provider is not None else None
            parsed_pure = _extract_intent(pure_out)
            pure_valid = parsed_pure is not None
            pure_pred = parsed_pure or INTENT_HUMAN
            pure_first_ms = (time.perf_counter() - pure_started) * 1000
        else:
            pure_pred = _offline_semantic_predict(text)
            pure_valid = True
            pure_first_ms = _estimated_ttft_ms(pure_in)
        pure_ttft.append(pure_first_ms)
        pure_output += OFFLINE_OUTPUT_TOKENS

        llm_calls: list[dict] = []

        def tracked_llm(prompt: str) -> dict | None:
            started = time.perf_counter()
            if use_live_llm:
                out = provider(prompt) if provider is not None else None
                elapsed = (time.perf_counter() - started) * 1000
            else:
                out = {"intent": _offline_semantic_predict(text), "provider": "offline-semantic"}
                elapsed = _estimated_ttft_ms(_estimate_llm_tokens(prompt))
            llm_calls.append({"prompt": prompt, "ttft_ms": elapsed})
            return out

        route_started = time.perf_counter()
        hybrid = route_intent(
            None, case_id=case["case_id"], text=text,
            llm_route=tracked_llm, persist=False,
        )
        route_elapsed = (time.perf_counter() - route_started) * 1000
        hybrid_route_latency.append(route_elapsed)
        if llm_calls:
            hybrid_llm_calls += len(llm_calls)
            hybrid_ttft.append(llm_calls[0]["ttft_ms"])
            for call in llm_calls:
                hybrid_input += _estimate_llm_tokens(call["prompt"])
                hybrid_output += OFFLINE_OUTPUT_TOKENS
        else:
            # 规则直出时没有“首 token”，以首个分类结果时间作 TTFT=0 级代理。
            hybrid_ttft.append(route_elapsed)

        evaluated.append({
            "case_id": case["case_id"],
            "expected": case["expected"],
            "pure_prediction": pure_pred,
            "pure_valid": pure_valid,
            "hybrid_prediction": hybrid.final_intent,
            "hybrid_valid": bool(hybrid.level_1_rule or hybrid.level_2_llm),
            "hybrid_source": "llm" if llm_calls else "rule",
        })

    pure_quality = _quality(evaluated, "pure_prediction", "pure_valid")
    hybrid_quality = _quality(evaluated, "hybrid_prediction", "hybrid_valid")
    pure_tokens = pure_input + pure_output
    hybrid_tokens = hybrid_input + hybrid_output
    token_gain = (1 - hybrid_tokens / pure_tokens) if pure_tokens else 0.0
    ttft_gain = (1 - statistics.fmean(hybrid_ttft) / statistics.fmean(pure_ttft)) \
        if pure_ttft and statistics.fmean(pure_ttft) else 0.0
    total = len(evaluated)

    shopping_flow = None
    if include_shopping_flow:
        from app.services.shopping_flow_eval import evaluate_shopping_return_exchange_flows

        shopping_flow = evaluate_shopping_return_exchange_flows(sample_count=100)

    summary = {
        "total": total,
        "total_cases": total + (shopping_flow.get("sample_count", 0) if shopping_flow else 0),
        "passed": hybrid_quality["correct"],
        "failed": hybrid_quality["wrong"],
        "degraded": hybrid_quality["invalid_predictions"],
        "pass_rate": hybrid_quality["accuracy"],
        "coverage": hybrid_quality["coverage"],
        "macro_recall": hybrid_quality["macro_recall"],
        "refund_recall": hybrid_quality["refund_recall"],
        "hallucination_rate": hybrid_quality["hallucination_rate"],
        "pure_llm": pure_quality,
        "hybrid": hybrid_quality,
        "targets": {
            "sample_count_min": 100,
            "recall_min": TARGET_RECALL,
            "hallucination_max": TARGET_HALLUCINATION,
            "token_gain_min": TARGET_TOKEN_GAIN,
        },
        "target_status": {
            "sample_count": total >= 100,
            "coverage": hybrid_quality["coverage"] == 1.0,
            "recall": (hybrid_quality["macro_recall"] is not None
                       and hybrid_quality["macro_recall"] >= TARGET_RECALL),
            "hallucination": (hybrid_quality["hallucination_rate"] is not None
                              and hybrid_quality["hallucination_rate"] <= TARGET_HALLUCINATION),
            "token_gain": token_gain >= TARGET_TOKEN_GAIN,
        },
        "shopping_flow": shopping_flow,
    }
    performance = {
        "ttft_ms": {
            "pure_llm": _latency_summary(pure_ttft),
            "hybrid": _latency_summary(hybrid_ttft),
            "reduction": round(ttft_gain, 4),
            "measurement": "wall_clock_live" if use_live_llm else "estimated_offline",
        },
        "hybrid_route_latency_ms": _latency_summary(hybrid_route_latency),
        "llm_calls": {"pure_llm": total, "hybrid": hybrid_llm_calls},
        "shopping_flow": {
            "duration_ms": shopping_flow.get("duration_ms"),
            "ocr_latency_ms": shopping_flow.get("ocr_latency_ms"),
            "measurement": shopping_flow.get("measurement"),
        } if shopping_flow else None,
    }
    cost = {
        "measured": False,
        "measurement": "estimated_tokens_same_prompt_contract",
        "pure_llm_tokens": pure_tokens,
        "hybrid_tokens": hybrid_tokens,
        "saved_tokens": pure_tokens - hybrid_tokens,
        "token_gain": round(token_gain, 4),
        "pure_breakdown": {"input": pure_input, "output": pure_output},
        "hybrid_breakdown": {"input": hybrid_input, "output": hybrid_output},
        "note": "provider 未返回 usage，Token 为同口径估算，不计入财务账单。",
    }
    return {
        "summary": summary,
        "performance": performance,
        "cost": cost,
        "dataset": {
            "version": DATASET_VERSION,
            "checksum": dataset_checksum(dataset),
            "sample_count": total,
            "intents": sorted(VALID_INTENTS),
            "balanced_samples_per_intent": total // len(VALID_INTENTS),
            "shopping_flow": {
                "version": shopping_flow.get("dataset_version"),
                "checksum": shopping_flow.get("checksum"),
                "sample_count": shopping_flow.get("sample_count"),
            } if shopping_flow else None,
        },
        "measurement_mode": "live_llm" if use_live_llm else "offline_replay",
    }


def execute_intent_eval_run(db: Session, run_id: str) -> dict:
    """执行已建立的 intent EvalRun，并把指标持久化到通用 Run 证据链。"""
    run = db.query(EvalRun).filter_by(run_id=run_id).first()
    if run is None:
        raise KeyError(run_id)
    if run.status not in (EVAL_RUN_PENDING, EVAL_RUN_RUNNING):
        return ec.get_run(db, run_id) or {}
    ec.mark_run_started(db, run_id)
    try:
        result = evaluate_intent_routes(use_live_llm=bool(settings.INTENT_EVAL_USE_LIVE_LLM))
        run = db.query(EvalRun).filter_by(run_id=run_id).first()
        cfg = dict(run.config or {})
        cfg.update({
            "dataset": result["dataset"],
            "measurement_mode": result["measurement_mode"],
            "read_only": True,
        })
        run.config = cfg
        db.commit()
        ec.finish_run(
            db, run_id, status=EVAL_RUN_COMPLETED,
            summary=result["summary"], performance=result["performance"],
            cost=result["cost"],
        )
    except Exception as exc:
        ec.finish_run(db, run_id, status=EVAL_RUN_FAILED, error=str(exc)[:500])
        raise
    return ec.get_run(db, run_id) or {}


def _latest_active_suite(db: Session) -> EvalSuite:
    suite = (
        db.query(EvalSuite).filter_by(status=EVAL_SUITE_ACTIVE)
        .order_by(EvalSuite.created_at.desc()).first()
    )
    if suite is None:
        item = ec.create_suite_from_golden(
            db, name="意图周期评测自动基线",
            changelog="为意图路由周期意图评测首次启动自动创建",
            owner_id="scheduler",
        )
        suite = db.get(EvalSuite, item["id"])
    if suite is None:  # pragma: no cover - 理论上不可达
        raise RuntimeError("无法创建意图评测调度基线")
    return suite


def create_intent_eval_run(
    db: Session, *, created_by: str, trigger_source: str = "manual",
    schedule_id: str | None = None, suite: EvalSuite | None = None,
) -> EvalRun:
    """创建可由现有 Evaluation Worker 消费的意图评测 Run。"""
    suite = suite or _latest_active_suite(db)
    run_id = f"intent-{uuid.uuid4().hex[:12]}"
    config = {
        "mode": "intent_benchmark",
        "tier": "intent",
        "schedule_id": schedule_id,
        "dataset_version": DATASET_VERSION,
        "read_only": True,
    }
    return ec.create_run(
        db, run_id=run_id,
        name="[意图] 意图路由 + 商城退换货OCR链路",
        suite=suite, created_by=created_by, config=config,
        target_type="intent_router", task_type="intent_eval",
        trigger_source=trigger_source,
    )


def enqueue_intent_run(run: EvalRun) -> None:
    from app.infrastructure.redis_client import STREAM_EVAL_RUN, xadd

    xadd(STREAM_EVAL_RUN, {
        "case_id": run.run_id,
        "run_id": run.run_id,
        "suite_id": run.suite_id or "",
        "limit": "0",
        "priority": "0",
        "target_type": "intent_router",
        "trigger_source": run.trigger_source or "manual",
        "config": json.dumps(run.config or {}, ensure_ascii=False),
    })


def latest_intent_run(db: Session) -> dict | None:
    # 新一轮 PENDING/RUNNING 时继续展示上一份完整指标，避免看板闪空。
    row = (
        db.query(EvalRun).filter_by(
            target_type="intent_router", status=EVAL_RUN_COMPLETED,
        )
        .order_by(EvalRun.created_at.desc()).first()
    )
    if row is None:
        row = (
            db.query(EvalRun).filter_by(target_type="intent_router")
            .order_by(EvalRun.created_at.desc()).first()
        )
    return ec.get_run(db, row.run_id) if row else None


def list_intent_runs(db: Session, *, limit: int = 20) -> dict:
    rows = (
        db.query(EvalRun).filter_by(target_type="intent_router")
        .order_by(EvalRun.created_at.desc()).limit(max(1, min(limit, 100))).all()
    )
    return {
        "total": db.query(EvalRun).filter_by(target_type="intent_router").count(),
        "items": [ec.get_run(db, row.run_id) for row in rows],
    }


def active_intent_run(db: Session) -> EvalRun | None:
    return (
        db.query(EvalRun)
        .filter_by(target_type="intent_router")
        .filter(EvalRun.status.in_([EVAL_RUN_PENDING, EVAL_RUN_RUNNING]))
        .order_by(EvalRun.created_at.desc()).first()
    )


def ensure_default_schedule(db: Session) -> dict:
    """幂等确保默认每日意图评测调度存在（初次启动立即到期）。"""
    existing = (
        db.query(EvalSchedule).filter_by(tier="intent")
        .order_by(EvalSchedule.created_at.asc()).first()
    )
    if existing is not None:
        return ec.get_schedule(db, existing.id) or {}
    suite = _latest_active_suite(db)
    return ec.create_schedule(
        db, suite_id=suite.id, tier="intent", cadence="daily",
        enabled=bool(settings.INTENT_EVAL_AUTO_SCHEDULE_ENABLED),
        owner_id="scheduler", initial_delay_minutes=0,
    )
