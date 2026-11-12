# -*- coding: utf-8 -*-
"""生产 Trace 漂移监测（V3.0 -评测 / V3-2 持续评测与成本治理）。

职责：
- 对生产/回归 Trace 做**低比例脱敏抽样**（比例合法性收敛，绝不全量扫描留存敏感字段）；
- 聚合窗口指标：意图热度 / 决策分布 / 人工推翻率 / 模型错误率；
- 与上一窗口基线对比，超阈值落 `eval_drift_indicators`（复用既有 M20 表）+ DRIFT 告警；
- 纯观测 + add-only：绝不改写任何退款/决策/评测状态；绝不落明文 PII。

红线（对齐 / V2 观测红线）：
- 只写 eval_drift_indicators / eval_alerts；
- 抽样输入必须先脱敏（`mask_pii`），指标 detail 不含原始诉求/订单/手机号；
- degraded / 未测量样本不当作通过；样本太少（<MIN_SAMPLES）如实标注 not_measured。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models import EvalAlert, EvalDriftIndicator, new_uuid

MIN_SAMPLES = 5                          # 不足此量 -> 数据不足，不做漂移判定（如实）
MAX_RATIO = 0.5                          # 抽样比例上限（绝对不允许半数以上留痕）
DEFAULT_RATIO = 0.05                     # 生产默认低比例
DEFAULT_DRIFT_PCT = 0.15                 # 相对基线漂移判定的基准阈值（15%）

_ORDER_PAT = re.compile(r"\bORD\s*[-_]?\s*\d{3,}\b")
_PHONE_PAT = re.compile(r"1[3-9]\d{9}")
_ID_PAT = re.compile(r"\b[0-9a-f]{8}[-]?[0-9a-f]{4}[-]?[0-9a-f]{4}[-]?[0-9a-f]{4}[-]?[0-9a-f]{12}\b", re.I)


@dataclass
class DriftSample:
    """一条脱敏前 Trace 摘要（入库前必须过 mask_pii）。"""
    input_text: str | None = None          # 诉求文本（可能含 PII，必须脱敏）
    intent: str | None = None              # 路由意图（非 PII，保留）
    decision: str | None = None            # APPROVED / REJECTED / SUSPENDED / ...
    human_override: bool | None = None     # 人工是否推翻
    model_error: bool | None = None        # 模型是否报错/退化
    source: str = "production"             # production / regression
    extra: dict = field(default_factory=dict)   # 其他命名指标（转 metric_key = prod/extra/<k>）


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def mask_pii(sample: DriftSample | dict) -> dict:
    """PII 脱敏：订单号/手机号/uuid → 遮蔽；长文本截断。纯函数，可测。"""
    if isinstance(sample, dict):
        d = dict(sample)
        text = d.get("input_text") or d.get("reason") or ""
    else:
        text = sample.input_text or ""
    text = _ORDER_PAT.sub("ORD-****", text)
    text = _PHONE_PAT.sub("138****0000", text)
    text = _ID_PAT.sub("uuid-****", text)
    text = text[:500]
    extra = dict((sample.extra if isinstance(sample, DriftSample) else {}) or {})
    return {
        "input_text": text,
        "intent": sample.intent if isinstance(sample, DriftSample) else d.get("intent"),
        "decision": (sample.decision if isinstance(sample, DriftSample)
                     else d.get("decision")),
        "human_override": (sample.human_override if isinstance(sample, DriftSample)
                           else d.get("human_override")),
        "model_error": (sample.model_error if isinstance(sample, DriftSample)
                        else d.get("model_error")),
        "source": (sample.source if isinstance(sample, DriftSample)
                   else d.get("source") or "production"),
        "extra": extra,
    }


def sample_production(candidates: list[DriftSample], ratio: float = DEFAULT_RATIO
                      ) -> list[DriftSample]:
    """确定性低比例抽样（同一批输入结果稳定；比例非法直接抛错，防误配全量留痕）。"""
    if not 0 < ratio <= MAX_RATIO:
        raise ValueError(f"抽样比例须在 (0, {MAX_RATIO}] 之间，收到 {ratio}")
    if not candidates:
        return []
    picked = []
    for s in candidates:
        h = hashlib.sha256((s.source + (s.input_text or "")).encode("utf-8")).hexdigest()
        if int(h[:8], 16) / 2 ** 32 <= ratio:
            picked.append(s)
    return picked


def _previous_value(db: Session, metric_key: str, *, exclude_run_id: str
                    ) -> float | None:
    """该指标上一窗口基线（排除本次 run_id 自身，避免同秒 created_at 取到刚写行）。"""
    row = (db.query(EvalDriftIndicator)
           .filter(EvalDriftIndicator.metric_key == metric_key)
           .filter(EvalDriftIndicator.run_id != exclude_run_id)
           .order_by(EvalDriftIndicator.created_at.desc(),
                     EvalDriftIndicator.id.desc()).first())
    return float(row.value) if row is not None else None


def _is_drifted(value: float, baseline: float, pct: float) -> bool:
    if baseline == 0:
        return value > pct
    return abs(value - baseline) / baseline >= pct


def _alert(db: Session, *, metric_key: str, message: str) -> dict | None:
    """幂等告警：同 metric_key 未 ack 不重复提示（对齐 observability._alert）。"""
    exists = (db.query(EvalAlert)
              .filter_by(alert_type="DRIFT", run_id=None, suite_id=None,
                         metric_key=metric_key, acked=False).first())
    if exists:
        return None
    a = EvalAlert(id=new_uuid(), alert_type="DRIFT", run_id=None, suite_id=None,
                  metric_key=metric_key, message=message, severity="warning")
    db.add(a)
    db.flush()
    return {
        "id": a.id, "alert_type": a.alert_type, "metric_key": metric_key,
        "message": message, "severity": a.severity,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _window_metrics(samples: list[DriftSample]) -> dict[str, float]:
    """把抽样窗口聚合为可对比的比率指标（分母=有效样本数，不足则跳过）。"""
    n = len(samples)
    if n == 0:
        return {}
    out: dict[str, float] = {}
    dec = [s.decision for s in samples if s.decision]
    if dec:
        from collections import Counter
        for k, v in Counter(dec).items():
            out[f"prod/decision_{k.lower()}"] = round(v / n, 4)
    ints = [s.intent for s in samples if s.intent]
    if ints:
        from collections import Counter
        for k, v in Counter(ints).items():
            out[f"prod/intent_{k.lower()}"] = round(v / n, 4)
    hov = [s for s in samples if s.human_override is not None]
    if hov:
        out["prod/human_override"] = round(
            sum(1 for s in hov if s.human_override) / len(hov), 4)
    mer = [s for s in samples if s.model_error is not None]
    if mer:
        out["prod/model_error"] = round(
            sum(1 for s in mer if s.model_error) / len(mer), 4)
    extras: dict[str, float] = {}
    for s in samples:
        for k, v in (s.extra or {}).items():
            if isinstance(v, (int, float)):
                extras[k] = extras.get(k, 0.0) + float(v)
    for k, v in extras.items():
        out[f"prod/extra/{k.lower()}"] = round(v / n, 4)
    return out


def sweep(db: Session, samples: list[DriftSample], *,
          ratio: float = DEFAULT_RATIO, drift_pct: float = DEFAULT_DRIFT_PCT,
          run_id: str | None = None) -> dict:
    """一次生产 Trace 漂移巡检：脱敏抽样 -> 窗口聚合 -> 对比判定 -> 落指标/告警。

    只写 eval_drift_indicators / eval_alerts（add-only）；绝不触碰退款/决策。
    样本不足 MIN_SAMPLES 时如实返回 not_measured=True（决不当作达标）。
    """
    if not samples:
        return {"run_id": run_id, "sampled": 0, "records": 0,
                "alerts": [], "drifted": [], "not_measured": True,
                "note": "无抽样输入"}
    picked = sample_production(samples, ratio=ratio)
    rid = run_id or f"prod-{uuid.uuid4().hex[:10]}"
    if len(picked) < MIN_SAMPLES:
        # 脱敏后的样本不足：不判漂移，如实返回 not_measured（永不当作达标）
        return {"run_id": rid, "sampled": len(picked), "records": 0,
                "alerts": [], "drifted": [], "not_measured": True,
                "note": f"抽样后仅 {len(picked)} 条，不足 {MIN_SAMPLES} 条，不做漂移判定"}
    masked = [mask_pii(s) for s in picked]
    metrics = _window_metrics([DriftSample(
        input_text=m["input_text"], intent=m["intent"], decision=m["decision"],
        human_override=m["human_override"], model_error=m["model_error"],
        source=m["source"], extra=m["extra"]) for m in masked])
    drifted: list[str] = []
    alerts: list[dict] = []
    records = 0
    for mk, value in metrics.items():
        baseline = _previous_value(db, mk, exclude_run_id=rid)
        drifted_flag = False
        detail = {"window_samples": len(picked), "source": "production"}
        if baseline is not None:
            drifted_flag = _is_drifted(value, baseline, drift_pct)
            detail["baseline"] = baseline
            detail["direction"] = "up" if value >= baseline else "down"
        detail["sanitized"] = True
        db.add(EvalDriftIndicator(
            id=new_uuid(), run_id=rid, suite_id=None, metric_key=mk,
            value=value, baseline=baseline if baseline is not None else value,
            threshold=drift_pct,
            drifted=drifted_flag,
            detail=detail))
        records += 1
        if drifted_flag:
            drifted.append(mk)
            a = _alert(db, metric_key=f"drift:{mk}",
                       message=(f"[DRIFT] 生产 Trace {mk} {value} vs 基线 "
                                f"{baseline}（threshold {drift_pct}%，run {rid}）"))
            if a:
                alerts.append(a)
    db.commit()
    return {
        "run_id": rid, "sampled": len(picked), "records": records,
        "alerts": alerts, "drifted": drifted, "not_measured": False,
        "metrics": metrics, "note": "低比例脱敏抽样，指标已落 eval_drift_indicators",
    }


def production_drift_summary(db: Session, *, limit: int = 5) -> dict:
    """给治理页：最近几个窗口的生产 Trace 漂移摘要（只读）。"""
    rows = (db.query(EvalDriftIndicator)
            .filter(EvalDriftIndicator.metric_key.like("prod/%"))
            .order_by(EvalDriftIndicator.created_at.desc()).limit(limit).all())
    return {
        "items": [{
            "id": r.id, "run_id": r.run_id, "metric_key": r.metric_key,
            "value": float(r.value or 0), "baseline": float(r.baseline or 0),
            "threshold": float(r.threshold or 0), "drifted": bool(r.drifted),
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
        "note": "只读摘要；指标由 sweep() 低比例脱敏抽样产生，无明文 PII",
    }