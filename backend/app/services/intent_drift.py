# -*- coding: utf-8 -*-
"""意图模型治理（V3-4）。

职责：
- 分类器版本注册：rule/dict/lightweight/llm 各自独立版本导出（只读注册表 + 批准留痕），
  绝不自动切换线上路由（切换必须走既有发布门禁）。
- 意图质量指标（实测口径）：退赔意图召回 ≥90%、幻觉 ≤2%（反馈采样窗聚合）。
  样本不足/无数据 -> 如实返回 None（不假报达标）。
- 回放集：feedback ADOPTED -> 待审核回放（去重/限长/人工审核 APPROVED/SKIPPED）；
  APPROVED 仅表示「列入黄金集重建前的回放验证候选」，绝不自动改生产规则（V-R1 红线）。
- 漂移巡检：窗口指标 vs 基线 -> 落 eval_drift_indicators + 幂等告警（纯观测 add-only）。

与 eval_refund/observability 的区别：本模块只处理意图域闭环（版本/质量/回放/漂移），
不触碰退款/评测任务字段。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.domain.models import (
    CLS_VERSION_APPROVED,
    CLS_VERSION_DRAFT,
    CLS_VERSION_REJECTED,
    CLASSIFIERS,
    EvalAlert,
    EvalDriftIndicator,
    INTENT_REFUND,
    IntentClassifierVersion,
    IntentFeedbackSample,
    IntentReplayQueueItem,
    REPLAY_APPROVED,
    REPLAY_PENDING,
    REPLAY_SKIPPED,
    REPLAY_STATUSES,
    new_uuid,
)

# ---- 质量目标（实测口径）----
TARGET_RECALL_REFUND = 0.90        # 退赔意图召回 ≥90%
TARGET_HALLUCINATION = 0.02        # 幻觉率 ≤2%
MIN_FEEDBACK_FOR_MEASURE = 0       # 只要有一份反馈即可计算（诚实计数）

# 漂移判定阈值：相对基线变化 ≥ 该比例判漂移（对齐 drift_monitor.DEFAULT_DRIFT_PCT）
DEFAULT_DRIFT_PCT = 0.15
# 数据不足阈值：窗口内反馈样本不足则如实 not_measured（不当作达标）
MIN_SAMPLES_FOR_DRIFT = 5

# 回放集限长：一次入队最大条数，防止 ADOPTED 膨胀失控
REPLAY_ENQUEUE_LIMIT = 200


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sha256(obj: dict) -> str:
    canon = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


# =====================================================================
# 1. 分类器版本注册（rule/dict/lightweight/llm）
# =====================================================================

def register_classifier_version(
    db: Session, *, classifier_name: str, version: str,
    payload: dict, creator_id: str | None = None, changelog: str | None = None,
) -> dict:
    """登记一个分类器版本快照（DRAFT）。非法分类器/重复版本抛 ValueError。"""
    if classifier_name not in CLASSIFIERS:
        raise ValueError(f"非法分类器种类: {classifier_name}（须在 {CLASSIFIERS}）")
    dup = (db.query(IntentClassifierVersion)
           .filter_by(classifier_name=classifier_name, version=version).first())
    if dup is not None:
        raise ValueError(f"分类器版本已存在: {classifier_name}/{version}")
    row = IntentClassifierVersion(
        id=new_uuid(), classifier_name=classifier_name, version=version,
        status=CLS_VERSION_DRAFT, content_sha256=_sha256(payload or {}),
        payload=payload, changelog=changelog, creator_id=creator_id,
    )
    db.add(row)
    db.commit()
    return _cls_version_as_dict(row)


def approve_classifier_version(db: Session, *, reg_id: str,
                               approver_id: str | None = None) -> dict:
    """批准版本（仅 DRAFT/REJECTED -> APPROVED）。已批准不可重复批准。"""
    row = db.get(IntentClassifierVersion, reg_id)
    if row is None:
        raise KeyError(f"分类器版本 {reg_id} 不存在")
    if row.status not in (CLS_VERSION_DRAFT, CLS_VERSION_REJECTED):
        raise ValueError(f"非法流转：{row.status} -> APPROVED")
    row.status = CLS_VERSION_APPROVED
    row.approver_id = approver_id
    row.approved_at = _now_naive()
    db.commit()
    return _cls_version_as_dict(row)


def reject_classifier_version(db: Session, *, reg_id: str,
                              reviewer_id: str | None = None) -> dict:
    row = db.get(IntentClassifierVersion, reg_id)
    if row is None:
        raise KeyError(f"分类器版本 {reg_id} 不存在")
    if row.status == CLS_VERSION_APPROVED:
        raise ValueError("已批准版本不可否决（须走发布门禁评估）")
    row.status = CLS_VERSION_REJECTED
    row.approver_id = reviewer_id
    db.commit()
    return _cls_version_as_dict(row)


def list_classifier_versions(db: Session, *, classifier_name: str | None = None,
                             limit: int = 50) -> list[dict]:
    q = db.query(IntentClassifierVersion)
    if classifier_name is not None:
        q = q.filter_by(classifier_name=classifier_name)
    rows = (q.order_by(IntentClassifierVersion.created_at.desc(),
                       IntentClassifierVersion.id.desc()).limit(limit).all())
    return [_cls_version_as_dict(r) for r in rows]


def latest_approved_classifier_version(db: Session, classifier_name: str) -> dict | None:
    if classifier_name not in CLASSIFIERS:
        raise ValueError(f"非法分类器种类: {classifier_name}")
    row = (db.query(IntentClassifierVersion)
           .filter_by(classifier_name=classifier_name, status=CLS_VERSION_APPROVED)
           .order_by(IntentClassifierVersion.created_at.desc(),
                     IntentClassifierVersion.id.desc()).first())
    return _cls_version_as_dict(row) if row is not None else None


def _cls_version_as_dict(r: IntentClassifierVersion) -> dict:
    return {
        "id": r.id, "classifier_name": r.classifier_name, "version": r.version,
        "status": r.status, "content_sha256": r.content_sha256,
        "changelog": r.changelog, "creator_id": r.creator_id,
        "approver_id": r.approver_id,
        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# =====================================================================
# 2. 意图质量指标（实测口径）
# =====================================================================

def intent_eval_metrics(db: Session, *, window_days: int = 7,
                        case_prefix: str | None = None) -> dict:
    """窗口内意图质量（反馈采样聚合）。样本不足/无数据 -> 对应指标如实 None。

    - refund_recall：人工纠正=REFUND 的采样中，路由也判 REFUND 的占比（≥90% 目标）；
    - hallucination_rate：来源 llm/hybrid 的路由中，被人工纠正（确认误路由）占比（≤2% 目标）；
    - 任何分母为 0 -> None（未测得，绝不假报达标）。

    case_prefix：可选批次/来源隔离（如测试用前缀），按 case_id 前缀过滤，
    避免整窗计量被无关批次污染；None 时统计全局窗口。
    """
    window_days = max(1, min(window_days, 365))
    since = _now_naive() - timedelta(days=window_days)

    fb_q = db.query(IntentFeedbackSample).filter(
        IntentFeedbackSample.created_at >= since)
    if case_prefix:
        fb_q = fb_q.filter(IntentFeedbackSample.case_id.like(f"{case_prefix}%"))
    fb = fb_q.all()
    # 需要人工纠正意图来定义召回分母；路由来源需另查 intent_results（按 case_id）
    refund_den = [f for f in fb if f.human_corrected_intent == INTENT_REFUND]
    refund_hit = [f for f in refund_den if f.route_final_intent == INTENT_REFUND]
    refund_recall = (len(refund_hit) / len(refund_den)) if refund_den else None

    # 幻觉率：llm/hybrid 来源路由中，人工纠正≠路由（确认误路由）的占比
    from app.domain.models import IntentResult
    routed_q = db.query(IntentResult).filter(IntentResult.created_at >= since)
    if case_prefix:
        routed_q = routed_q.filter(IntentResult.case_id.like(f"{case_prefix}%"))
    routed = routed_q.all()
    src_of_case = {r.case_id: r.route_source for r in routed if r.case_id}
    llm_like = [f for f in fb
                if src_of_case.get(f.case_id) in ("llm", "hybrid")
                and f.human_corrected_intent]
    hallucinated = [f for f in llm_like
                    if f.route_final_intent != f.human_corrected_intent]
    hallucination_rate = (len(hallucinated) / len(llm_like)) if llm_like else None

    confirmed_miss = [f for f in fb
                      if f.status == "ADOPTED"
                      and f.route_final_intent != f.human_corrected_intent]

    return {
        "window_days": window_days,
        "feedback_total": len(fb),
        "refund_samples": len(refund_den),
        "refund_recall": refund_recall,
        "refund_recall_meets": (refund_recall is not None
                                and refund_recall >= TARGET_RECALL_REFUND),
        "llm_hybrid_routed": len(llm_like),
        "hallucination_rate": hallucination_rate,
        "hallucination_meets": (hallucination_rate is not None
                                and hallucination_rate <= TARGET_HALLUCINATION),
        "confirmed_misses": len(confirmed_miss),
        "targets": {"recall_refund": TARGET_RECALL_REFUND,
                    "hallucination": TARGET_HALLUCINATION},
    }


# =====================================================================
# 3. 回放集（feedback ADOPTED -> 待审核回放）
# =====================================================================

def enqueue_replay_from_feedback(db: Session, *, limit: int = REPLAY_ENQUEUE_LIMIT
                                 ) -> dict:
    """把 ADOPTED 但未入队的反馈采样入回放集（去重：sample_id 唯一，至多一行）。

    只入队、不改任何路由/规则；审核流转见 review_replay。
    """
    adopted = (db.query(IntentFeedbackSample)
               .filter_by(status="ADOPTED")
               .order_by(IntentFeedbackSample.created_at.desc()).all())
    queued = 0
    skipped = 0
    for fb in adopted:
        exists = (db.query(IntentReplayQueueItem)
                  .filter_by(sample_id=fb.id).first())
        if exists is not None:
            skipped += 1
            continue
        db.add(IntentReplayQueueItem(id=new_uuid(), sample_id=fb.id,
                                     status=REPLAY_PENDING))
        queued += 1
        if queued >= limit:
            db.commit()
            return {"queued": queued, "skipped": skipped,
                    "note": f"一次入队上限 {limit} 达限，其余留在 ADOPTED 中",
                    "limited": True}
    db.commit()
    return {"queued": queued, "skipped": skipped, "limited": queued >= limit}


def list_replay_queue(db: Session, *, status: str | None = None,
                      limit: int = 50) -> list[dict]:
    if status is not None and status not in REPLAY_STATUSES:
        raise ValueError(f"非法回放状态: {status}")
    q = (db.query(IntentReplayQueueItem, IntentFeedbackSample)
         .join(IntentFeedbackSample,
               IntentReplayQueueItem.sample_id == IntentFeedbackSample.id))
    if status is not None:
        q = q.filter(IntentReplayQueueItem.status == status)
    rows = (q.order_by(IntentReplayQueueItem.created_at.desc())
            .limit(limit).all())
    out = []
    for item, fb in rows:
        d = _replay_as_dict(item)
        d["sample"] = {
            "case_id": fb.case_id,
            "route_final_intent": fb.route_final_intent,
            "human_corrected_intent": fb.human_corrected_intent,
            "reason": fb.reason,
        }
        out.append(d)
    return out


def review_replay(db: Session, *, replay_id: str, decision: str,
                  reviewed_by: str | None = None) -> dict:
    """回放集审核：PENDING -> APPROVED / SKIPPED。不可跳级、不可重复。

    APPROVED 仅是「列入黄金集重建回放验证候选」的标记；实际重建仍走既有
    冻结流程，本调用绝不改写任何在线路由/规则。
    """
    if decision not in (REPLAY_APPROVED, REPLAY_SKIPPED):
        raise ValueError(f"非法审核决策: {decision}")
    item = db.get(IntentReplayQueueItem, replay_id)
    if item is None:
        raise KeyError(f"回放项 {replay_id} 不存在")
    if item.status != REPLAY_PENDING:
        raise ValueError(f"非待审核状态不可流转：{item.status}")
    item.status = decision
    item.reviewed_by = reviewed_by
    item.decided_at = _now_naive()
    db.commit()
    return _replay_as_dict(item)


def _replay_as_dict(item: IntentReplayQueueItem) -> dict:
    return {
        "id": item.id, "sample_id": item.sample_id,
        "status": item.status, "reviewed_by": item.reviewed_by,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


# =====================================================================
# 4. 漂移巡检（窗口指标 vs 基线，落 EvalDriftIndicator）
# =====================================================================

def _previous_value(db: Session, metric_key: str, *, exclude_run_id: str
                    ) -> float | None:
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
    """幂等告警（对齐 drift_monitor._alert）：同 metric_key 未 ack 不重复。"""
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


def sweep_intent_drift(db: Session, *, window_days: int = 7,
                       drift_pct: float = DEFAULT_DRIFT_PCT,
                       case_prefix: str | None = None) -> dict:
    """一次意图质量漂移巡检：聚合 -> 对比基线 -> 落指标 + 告警。

    采集指标：
    - intent/refund_recall      退赔召回（可空）
    - intent/hallucination_rate 幻觉率（可空，无 llm/hybrid 反馈则跳过）
    - intent/rule_hit_rate      规则命中占比（区别于漂移，纯分布观察）

    反馈不足 MIN_SAMPLES_FOR_DRIFT -> 如实 not_measured（不判漂移、不当作达标）。
    case_prefix：批次隔离（可选），与 intent_eval_metrics 同语义。
    """
    m = intent_eval_metrics(db, window_days=window_days, case_prefix=case_prefix)
    fb_total = m["feedback_total"]
    rid = f"intent-{uuid.uuid4().hex[:10]}"
    if fb_total < MIN_SAMPLES_FOR_DRIFT:
        return {
            "run_id": rid, "feedback_total": fb_total, "records": 0,
            "drifted": [], "alerts": [], "not_measured": True,
            "note": f"窗口反馈仅 {fb_total} 条 < {MIN_SAMPLES_FOR_DRIFT}，不做漂移判定",
        }

    metrics: dict[str, float] = {}
    # metric_key 按批次命名空间隔离（避免不同前缀数据集共享基线相互干扰）
    ns = ("." + case_prefix.strip(" -_")) if case_prefix else ""
    if m["refund_recall"] is not None:
        metrics[f"intent{ns}/refund_recall"] = round(m["refund_recall"], 4)
    if m["hallucination_rate"] is not None:
        metrics[f"intent{ns}/hallucination_rate"] = round(m["hallucination_rate"], 4)

    drifted: list[str] = []
    alerts: list[dict] = []
    records = 0
    for mk, value in metrics.items():
        baseline = _previous_value(db, mk, exclude_run_id=rid)
        off = _is_drifted(value, baseline if baseline is not None else value,
                          drift_pct) if baseline is not None else False
        db.add(EvalDriftIndicator(
            id=new_uuid(), run_id=rid, suite_id="intent", metric_key=mk,
            value=value, baseline=baseline if baseline is not None else value,
            threshold=drift_pct, drifted=off,
            detail={"window_days": window_days, "feedback": fb_total,
                    "targets": {"recall": TARGET_RECALL_REFUND,
                                "hallucination": TARGET_HALLUCINATION}}))
        records += 1
        if off:
            drifted.append(mk)
            a = _alert(db, metric_key=f"drift:{mk}",
                       message=(f"[DRIFT] 意图质量 {mk} {value} vs 基线 "
                                f"{baseline}（threshold {drift_pct}，run {rid}）"))
            if a:
                alerts.append(a)
    db.commit()
    return {
        "run_id": rid, "feedback_total": fb_total, "records": records,
        "drifted": drifted, "alerts": alerts, "not_measured": False,
        "metrics": metrics, "note": "窗口指标 vs 基线漂移判定（只读观测）",
    }


def intent_drift_summary(db: Session, *, limit: int = 5) -> dict:
    """给治理页：最近几轮意图质量漂移摘要（只读）。"""
    rows = (db.query(EvalDriftIndicator)
            .filter(EvalDriftIndicator.suite_id == "intent")
            .order_by(EvalDriftIndicator.created_at.desc()).limit(limit).all())
    return {
        "items": [{
            "id": r.id, "run_id": r.run_id, "metric_key": r.metric_key,
            "value": float(r.value or 0), "baseline": float(r.baseline or 0),
            "threshold": float(r.threshold or 0), "drifted": bool(r.drifted),
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
        "note": "只读摘要；漂移由 sweep_intent_drift() 产生，绝不反写路由规则",
    }