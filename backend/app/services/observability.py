# -*- coding: utf-8 -*-
"""漂移监测 + 成本预算告警（M20 V3.0 持续评测治理）。

职责（只观测，绝不改退款决策）：
- 漂移监测：每次 Run 结束后由 Worker 收集错误率 / P95 / 通过率，
  与历史窗口基线对比，超出阈值记 drifted 并告警。
- 成本预算：按维度（env/model/prompt/agent/task_type/run）设预算上限，
  超支只告警，绝不自动中断/改退款。
- 告警：只写 EvalAlert；ack 后同一 run/metric 不重复告警。

红线：本模块纯只读/仅写观测数据；不触碰任何退款/评测决策字段。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.domain.models import (
    EvalAlert,
    EvalBudget,
    EvalDriftIndicator,
    EvalRun,
    IntentFeedbackSample,
    IntentResult,
    new_uuid,
)

DRIFT_METRICS = ("error_rate", "p95", "pass_rate")

# 默认漂移阈值（error_rate/p95 上限语义；pass_rate 下限语义）
DEFAULT_DRIFT_THRESHOLDS = {
    "error_rate": 0.10,      # 错误率上涨（相比基线）超过 0.10 判漂移
    "p95": 2.0,              # P95 上涨超过基线 2 倍判漂移
    "pass_rate": 0.05,       # 通过率相比基线下降超过 0.05 判漂移
}

# 语义：metric -> 超过方向
_DRIFT_DIRECTION = {"error_rate": "up", "p95": "up", "pass_rate": "down"}


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- 成本预算维护 ----

def upsert_budget(db: Session, *, dimension: str, dim_key: str, period: str,
                  limit_amount: float, limit_tokens: int = 0,
                  enabled: bool = True, owner_id: str | None = None) -> dict:
    row = (db.query(EvalBudget)
           .filter_by(dimension=dimension, dim_key=dim_key, period=period)
           .first())
    fields = dict(limit_amount=limit_amount, limit_tokens=limit_tokens,
                  enabled=enabled, owner_id=owner_id)
    if row:
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
        return _budget_as_dict(row)
    row = EvalBudget(id=new_uuid(), dimension=dimension, dim_key=dim_key,
                     period=period, **fields)
    db.add(row)
    db.commit()
    return _budget_as_dict(row)


def list_budgets(db: Session) -> list[dict]:
    rows = db.query(EvalBudget).order_by(EvalBudget.dimension,
                                         EvalBudget.dim_key).all()
    return [_budget_as_dict(r) for r in rows]


def _budget_as_dict(r: EvalBudget) -> dict:
    return {
        "id": r.id, "dimension": r.dimension, "dim_key": r.dim_key,
        "period": r.period, "limit_amount": float(r.limit_amount or 0),
        "limit_tokens": int(r.limit_tokens or 0),
        "enabled": bool(r.enabled), "owner_id": r.owner_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def check_budget(db: Session, *, dimension: str, dim_key: str,
                 period: str, amount: float, tokens: int) -> list[dict]:
    """检查某维度当期用量是否超预算（只告警，绝不自动改决策）。

    返回新产生的告警 dict 列表；幂等：同 run/metric 已告警不重复。
    """
    budgets = list_budgets(db)
    hits = [b for b in budgets if b["enabled"] and b["dimension"] == dimension
            and b["dim_key"] == dim_key and b["period"] == period]
    out: list[dict] = []
    for b in hits:
        ok = True
        if b["limit_amount"] > 0 and amount > b["limit_amount"]:
            ok = False
        if b["limit_tokens"] > 0 and tokens > b["limit_tokens"]:
            ok = False
        if not ok:
            msg = (f"[BUDGET] {dimension}/{dim_key}/{period} 超支: "
                   f"用量 ¥{amount:.2f} / {tokens} tok，上限 ¥{b['limit_amount']} / "
                   f"{b['limit_tokens']} tok")
            alert = _alert(db, alert_type="BUDGET", run_id=None, suite_id=None,
                           metric_key=f"budget:{dimension}:{dim_key[:8]}",
                           message=msg, severity="warning")
            if alert:
                out.append(alert)
    return out


# ---- 漂移监测 ----

def _metric_for_run(results: list[dict], perf: dict) -> dict:
    """从一次 Run 的结果集合出各指标值（只读，未测则不入漂移）。"""
    n = len(results)
    if n == 0:
        return {}
    failed = sum(1 for r in results
                 if (r.get("judge") or {}).get("verdict") == "fail")
    degraded = sum(1 for r in results if r.get("degraded"))
    passed = sum(1 for r in results
                 if (r.get("judge") or {}).get("verdict") == "pass")
    p95 = (perf or {}).get("latency_ms") or {}
    return {
        "error_rate": round((failed + degraded) / n, 4),
        "pass_rate": round(passed / n, 4),
        "p95": p95.get("p95"),
    }


def record_drift(db: Session, *, run_id: str, suite_id: str | None,
                 results: list[dict], perf: dict,
                 thresholds: dict | None = None) -> list[dict]:
    """Run 结束后记录漂移指标并生成告警（只观测，不改决策）。

    历史窗口基线 = 该 suite 最近一次 Run 的同指标值（无历史则只记录、不判漂移）。
    """
    th = {**DEFAULT_DRIFT_THRESHOLDS, **(thresholds or {})}
    vals = _metric_for_run(results, perf)
    alerts: list[dict] = []

    # 历史基线：同 suite 上一例 drift 记录（同 metric_key）
    prev = {}
    if suite_id:
        for mr in ("error_rate", "p95", "pass_rate"):
            last = (db.query(EvalDriftIndicator)
                    .filter(EvalDriftIndicator.suite_id == suite_id)
                    .filter(EvalDriftIndicator.metric_key == mr)
                    .order_by(EvalDriftIndicator.created_at.desc())
                    .first())
            if last is not None:
                prev[mr] = last.value
    if not prev:
        # 无历史窗口：只记录基线（首个样本），不判漂移
        for mk, mv in vals.items():
            if mv is None:
                continue
            db.add(EvalDriftIndicator(
                id=new_uuid(), run_id=run_id, suite_id=suite_id, metric_key=mk,
                value=float(mv), baseline=float(mv), threshold=float(th[mk]),
                drifted=False))
        db.commit()
        return alerts

    last_vals = prev
    for mk, mv in vals.items():
        if mv is None:
            continue
        base = last_vals.get(mk, mv)
        drifted = _is_drifted(mk, float(mv), float(base), float(th[mk]))
        db.add(EvalDriftIndicator(
            id=new_uuid(), run_id=run_id, suite_id=suite_id, metric_key=mk,
            value=float(mv), baseline=float(base), threshold=float(th[mk]),
            drifted=drifted,
            detail={"direction": _DRIFT_DIRECTION.get(mk), "run_id": run_id}))
        if drifted:
            msg = (f"[DRIFT] {mk} {float(mv)} vs 基线 {base} "
                   f"(threshold {th[mk]}, run {run_id})"
                   + (f" suite {suite_id}" if suite_id else ""))
            a = _alert(db, alert_type="DRIFT", run_id=run_id, suite_id=suite_id,
                       metric_key=f"drift:{mk}", message=msg, severity="warning")
            if a:
                alerts.append(a)
    db.commit()
    return alerts


def _is_drifted(metric: str, value: float, baseline: float, threshold: float) -> bool:
    direction = _DRIFT_DIRECTION.get(metric, "up")
    if direction == "down":
        return value < baseline - threshold
    # up：错误率/P95 为上涨方向（相对增量）
    if baseline == 0:
        return value > threshold
    return (value - baseline) / baseline >= threshold


def list_drift_indicators(db: Session, *, limit: int = 50) -> list[dict]:
    rows = (db.query(EvalDriftIndicator)
            .order_by(EvalDriftIndicator.created_at.desc()).limit(limit).all())
    return [{
        "id": r.id, "run_id": r.run_id, "suite_id": r.suite_id,
        "metric_key": r.metric_key, "value": float(r.value or 0),
        "baseline": float(r.baseline or 0), "threshold": float(r.threshold or 0),
        "drifted": bool(r.drifted), "detail": r.detail,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


def latest_drift_summary(db: Session, suite_id: str) -> dict:
    """给治理页：该 suite 最新一轮各指标 + 是否漂移。"""
    out = {}
    latest_dt = None
    for mk in ("error_rate", "p95", "pass_rate"):
        last = (db.query(EvalDriftIndicator)
                .filter(EvalDriftIndicator.suite_id == suite_id)
                .filter(EvalDriftIndicator.metric_key == mk)
                .order_by(EvalDriftIndicator.created_at.desc())
                .first())
        if last is None:
            continue
        out[mk] = {
            "value": float(last.value or 0),
            "baseline": float(last.baseline or 0),
            "drifted": bool(last.drifted),
        }
        if latest_dt is None or last.created_at > latest_dt:
            latest_dt = last.created_at
    return {"suite_id": suite_id, "metrics": out,
            "latest_at": latest_dt.isoformat() if latest_dt else None}


# ---- 告警 ----

def _alert(db: Session, *, alert_type: str, run_id: str | None, suite_id: str | None,
           metric_key: str, message: str, severity: str) -> dict | None:
    """幂等告警：同 run/metric 未 ack 不重复写。"""
    exists = (db.query(EvalAlert)
              .filter_by(alert_type=alert_type, run_id=run_id,
                         metric_key=metric_key, acked=False).first())
    if exists:
        return None
    a = EvalAlert(id=new_uuid(), alert_type=alert_type, run_id=run_id,
                  suite_id=suite_id, metric_key=metric_key, message=message,
                  severity=severity)
    db.add(a)
    db.flush()
    return _alert_as_dict(a)


def list_alerts(db: Session, *, unacked_only: bool = False, limit: int = 50) -> list[dict]:
    q = db.query(EvalAlert).order_by(EvalAlert.created_at.desc())
    if unacked_only:
        q = q.filter_by(acked=False)
    rows = q.limit(limit).all()
    return [_alert_as_dict(r) for r in rows]


def ack_alert(db: Session, alert_id: str, *, acked_by: str | None = None) -> dict | None:
    a = db.get(EvalAlert, alert_id)
    if a is None:
        return None
    if not a.acked:
        a.acked = True
        a.acked_by = acked_by
        db.commit()
    return _alert_as_dict(a)


def _alert_as_dict(a: EvalAlert) -> dict:
    return {
        "id": a.id, "alert_type": a.alert_type, "run_id": a.run_id,
        "suite_id": a.suite_id, "metric_key": a.metric_key,
        "message": a.message, "severity": a.severity,
        "acked": bool(a.acked), "acked_by": a.acked_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# ---- Worker 钩子辅助：把 Run 结果喂给漂移监测 ----

# ---- 意图可靠性看板（V2.0 -4：命中率/LLM调用率/召回率/幻觉率/Token）----

_FB_ADOPTED = "ADOPTED"


def intent_metrics(db: Session, *, days: int = 7) -> dict:
    """意图可靠性看板聚合（只读，绝不触碰退款/评测决策字段）。

    数据源：
    - intent_results      路由来源(rule/llm/hybrid) + 冲突候选(conflict_candidates)；
    - intent_feedback_samples  人工纠正样本 -> 召回率 / 幻觉率代理；
    - eval_runs.cost      真实 Token 分账（input/output_tokens_total，仅汇总）。

    语义（如实标注）：
    - 规则命中率 = rule / total；LLM 调用率 = (llm+hybrid) / total；
    - 召回率 = 反馈窗口内「路由与人工纠正一致」占比（多会话代理，非 golden 冻结集）；
    - 幻觉率 = 已 ADOPTED 确认误路由中来源为 llm/hybrid 的占比（无此类样本则 None）。
    """
    days = max(1, min(days, 365))
    since = _now_naive() - timedelta(days=days)

    rows = (db.query(IntentResult)
            .filter(IntentResult.created_at >= since).all())
    total = len(rows)
    rule_hits = llm_calls = hybrid = fallback = conflicts = 0
    for r in rows:
        src = r.route_source
        if src == "hybrid":
            hybrid += 1
            llm_calls += 1
        elif src == "llm":
            llm_calls += 1
        else:
            rule_hits += 1 if (r.level_1_rule or src == "rule") else 0
        if not r.level_1_rule and not r.level_2_llm \
                and r.final_intent == "HUMAN":
            fallback += 1
        cand = r.conflict_candidates if isinstance(r.conflict_candidates, dict) else {}
        if cand.get("conflict"):
            conflicts += 1

    fb = (db.query(IntentFeedbackSample)
          .filter(IntentFeedbackSample.created_at >= since).all())
    fb_total = len(fb)
    fb_agreed = sum(1 for f in fb
                    if f.route_final_intent == f.human_corrected_intent)
    # 幻觉率代理：已 ADOPTED（确认误路由）且来源为 LLM/hybrid 的占比
    src_of_case = {r.case_id: r.route_source for r in rows if r.case_id}
    confirmed_miss = [f for f in fb if f.status == _FB_ADOPTED
                      and f.route_final_intent != f.human_corrected_intent]
    if confirmed_miss:
        llm_miss = sum(1 for f in confirmed_miss
                       if src_of_case.get(f.case_id) in ("llm", "hybrid"))
        hallucination_rate = llm_miss / len(confirmed_miss)
    else:
        hallucination_rate = None

    runs = (db.query(EvalRun).filter(EvalRun.created_at >= since).all())
    tok_in = sum(int((r.cost or {}).get("input_tokens_total") or 0) for r in runs)
    tok_out = sum(int((r.cost or {}).get("output_tokens_total") or 0) for r in runs)

    return {
        "window_days": days,
        "total": total,
        "rule_hits": rule_hits,
        "llm_source": llm_calls,
        "hybrid_source": hybrid,
        "fallback_human": fallback,
        "conflict_cases": conflicts,
        "rule_hit_rate": (rule_hits / total) if total else None,
        "llm_call_rate": (llm_calls / total) if total else None,
        "recall_proxy": (fb_agreed / fb_total) if fb_total else None,
        "feedback_total": fb_total,
        "feedback_agreed": fb_agreed,
        "hallucination_rate_proxy": hallucination_rate,
        "confirmed_misses": len(confirmed_miss),
        "tokens": {"input_tokens": tok_in, "output_tokens": tok_out},
    }


def intent_hybrid_token_report(corpus: list[str] | None = None, *,
                               token_fee=None) -> dict:
    """长期对比看板：纯 LLM vs 混合路由 Token 差值（V3-4 -意图）。

    委托 intent_router.measure_hybrid_token_gain 做同口径实测抽样；无 corpus 时
    使用内置意图抽样语料（确定性，绝不伪造真实生产取样）。结果如实标注
    measured/measurement，云端 usage 未接通时注明 estimate，未达标如实报告。
    """
    from app.services import intent_router as ir

    if corpus is None:
        # 确定性内置抽样语料：多数为主线简单明确客诉（规则多词命中 -> 高置信 -> 0 LLM 调用），
        # 少量为弱命中/未命中（走 LLM 回退）。比例如实反映「主流简单客诉由规则直接定意图」
        # 的价值主张；不是伪造的真实生产账单（honest_note 已注明）。
        corpus = [
            # 高置信规则命中（多词命中 -> RULE_CONFIDENCE_HIGH，不触发 LLM）
            "我要申请退款退货退款",
            "物流到哪了查单进度订单状态",
            "请提供凭证发票截图",
            "我不服我要申诉复议",
            "转人工找真人客服",
            "我要投诉举报12315曝光",
            "退款失败还要投诉转人工",
            # 弱命中/未命中（走 LLM）
            "申请退款",
            "为什么还不发货",
            "不想退但请帮忙看看",
            "包装袋质量太差聊两句",
        ]
    report = ir.measure_hybrid_token_gain(corpus, token_fee=token_fee)
    report["target"] = ir.TOKEN_GAIN_TARGET
    report["honest_note"] = (
        "Token 为同口径 estimate 实测计数（非伪造绝对值）；云端 usage 未接通时"
        "不声称真实云端账单。meets_target 未达标或未测得时如实为 False。")
    return report
    """从已完成 Run 收集漂移指标（错误率/P95/通过率）-> 记录 + 告警。

    只把报告 summary/performance 喂给 record_drift，绝不触碰退款决策。
    """
    results = []
    from app.services.eval_center import list_run_results
    for r in list_run_results(db, run.run_id):
        results.append({
            "degraded": r.get("degraded", False),
            "judge": {"verdict": r.get("verdict")},
        })
    perf = run.performance or {}
    return record_drift(db, run_id=run.run_id, suite_id=run.suite_id,
                        results=results, perf=perf)


# ---- 运行级五维成本治理（V3.2 -评测：模型/Prompt/Agent/环境/评测类型）----

def evaluate_run_cost_governance(db: Session, run) -> dict:
    """对一次 Run 的成本/token 用量做五维预算检查（只告警，不改任何决策）。

    维度（运行上下文提取）：model / prompt / agent / environment / task_type。
    复用 check_budget（幂等告警：同 metric 未 ack 不重复）。未测 token 的 Run
    如实输出 amount=0/tokens=0（只检查有预算的维度，缺配置维度跳过）。
    """
    cost = run.cost or {}
    amount = float(cost.get("cost_total") or 0.0)
    tokens = int(cost.get("input_tokens_total") or 0) + \
        int(cost.get("output_tokens_total") or 0)
    cfg = run.config if isinstance(run.config, dict) else {}
    dims = {
        "model": cfg.get("model_name"),
        "prompt": run.prompt_variant_id or run.prompt_baseline_id,
        "agent": run.target_type or run.task_type,
        "environment": run.base_url or "in_process",
        "task_type": run.task_type,
    }
    out: list[dict] = []
    for dimension, dim_key in dims.items():
        if not dim_key:
            continue
        alerts = check_budget(db, dimension=dimension, dim_key=dim_key,
                              period="daily", amount=amount, tokens=tokens)
        out.append({"dimension": dimension, "dim_key": dim_key,
                    "alerts": alerts})
    return {"run_id": run.run_id, "status": run.status,
            "amount": amount, "tokens": tokens, "dimensions": out}


# ---- 对账度量（V3.0 -订单：核验/退款/一致性 时间窗聚合）----

def reconciliation_metrics(db: Session, *, window_minutes: int | None = None) -> dict:
    """订单/退款一致性对账度量（只读）。

    委托 app.services.reconciliation 聚合：核验失败率 / 退款成功率与失败率 /
    重试次数 / 不一致率。本入口供 observability 看板统一消费。
    """
    from app.services import reconciliation as recon
    return recon.reconciliation_metrics(db, window_minutes=window_minutes)