# -*- coding: utf-8 -*-
"""订单/退款一致性自动对账（V3.0 ）。

职责（全程只读判定 + 只写审计，绝不改任何退款/决策状态）：
- 把「工单/决策/退款状态」与「订单源 / 退款回执」做一致性 diff：
    * decision   —— 资金红线：已 APPROVED+已退 而核验未全 MATCH / 未批准却处于退后态；
    * verify     —— 订单/商品/价格核验与订单源现况漂移（订单消失/可退额恢复）；
    * amount     —— 申请金额超过订单可退金额；
    * refund_status —— case.refund_status 与 refund_events / refund_transactions 回执轨迹不符。
- 差异落 `reconciliation_events`（append-only 审计）：
    * guid = sha256(run_id:case:category:field) —— 同 run 同差异幂等，重跑不重复落审计；
    * severity 分级 CRITICAL/HIGH/WARNING/INFO，HIGH+ = 告警级（overview/alerts）。
-「差异 → 告警 + 审计事件」由本服务产出（告警即 reconciliation_events severity>=HIGH，
  resolved 由主管处置置位；审计事件另落 domain_events RECONCILIATION_RUN，append-only）。

红线（与既有金流守线一致）：
- 本模块不改变任何退款/评测/安全判定；只观察 + 落审计；
- 金额 diff 绝不自动"修正"案件；核验红线项绝不自动退款；
- provider 不可判定（超时/熔断降级）→ 如实记 UNKNOWN，绝不伪造默认值。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.models import (
    RECON_SEV_CRITICAL,
    RECON_SEV_HIGH,
    RECON_SEV_INFO,
    RECON_SEV_WARNING,
    DECISION_APPROVED,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_SUCCESS,
    ReconciliationEvent,
    RefundCase,
    RefundEvent,
    RefundTransaction,
    new_uuid,
)

GATE_PASS = "PASS"

# 对账类别
CAT_DECISION = "decision"
CAT_VERIFY = "verify"
CAT_AMOUNT = "amount"
CAT_REFUND_STATUS = "refund_status"

_VERIFY_FIELDS = ("order_verification", "product_verification", "price_verification")
_FINISHED_REFUND = (REFUND_STATUS_PENDING, REFUND_STATUS_SUCCESS)

_PRICE_TOLERANCE = 0.01  # 金额容差（订单可退金额对比），与 order_provider.PRICE_TOLERANCE_YUAN 对齐


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sha256_key(*parts: object) -> str:
    canon = json.dumps([str(p) for p in parts], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# =====================================================================
# 单案核对（纯函数式，便于测试）
# =====================================================================

def _verify_map(case: RefundCase) -> dict:
    return {f: getattr(case, f, None) for f in _VERIFY_FIELDS}


def _refund_events_for(db: Session, case_id: str) -> list[RefundEvent]:
    return (db.query(RefundEvent)
            .filter_by(case_id=case_id)
            .order_by(RefundEvent.created_at)
            .all())


def _latest_transaction(db: Session, case_id: str) -> RefundTransaction | None:
    return (db.query(RefundTransaction)
            .filter_by(case_id=case_id)
            .order_by(RefundTransaction.created_at.desc())
            .first())


def check_case(db: Session, *, case: RefundCase, provider=None) -> list[dict]:
    """对单个案件跑全部对账检查（不落库）。返回 diff 描述 list。

    provider 不传时用全局订单源（确定性 Mock）；传 MultiSourceOrderProvider 可复用
    多来源聚合（其各来源判定由调用方另行落 OrderSourceVerdict）。
    """
    if provider is None:
        from app.infrastructure.order_provider import get_order_provider
        provider = get_order_provider()

    diffs: list[dict] = []
    vm = _verify_map(case)
    all_match = all(v == "MATCH" for v in vm.values())

    # ---- decision：资金红线 ----
    if case.final_decision == DECISION_APPROVED and case.refund_status in _FINISHED_REFUND:
        if not all_match:
            diffs.append(dict(
                category=CAT_DECISION, field="refund_without_match_verify",
                severity=RECON_SEV_CRITICAL,
                expected={"verify_all_match": True, "final_decision": "APPROVED"},
                actual=vm,
                diff_detail="已批准并进入退后态，但订单/商品/价格核验未全部 MATCH（资金红线）",
            ))
    elif case.final_decision == DECISION_APPROVED and case.refund_status not in _FINISHED_REFUND:
        if all(v is None for v in vm.values()):
            diffs.append(dict(
                category=CAT_DECISION, field="approved_without_verify",
                severity=RECON_SEV_WARNING,
                expected={"verify_snapshot": "存在核验快照"},
                actual=vm,
                diff_detail="已批准但无任何核验快照（流程遗留或快照缺失）",
            ))
    if case.refund_status in _FINISHED_REFUND and case.final_decision != DECISION_APPROVED:
        diffs.append(dict(
            category=CAT_DECISION, field="refund_without_approval",
            severity=RECON_SEV_CRITICAL,
            expected={"final_decision": "APPROVED"},
            actual={"final_decision": case.final_decision, "refund_status": case.refund_status},
            diff_detail="处于退后态但最终决策未 APPROVED（绕过审批红线）",
        ))

    # ---- verify：订单源漂移（快照与现况对比）----
    from app.services.order_lookup import latest_snapshot, REASON_NO_REFUNDABLE
    snap = latest_snapshot(db, case.id)
    if snap is not None:
        try:
            pr = provider.lookup(case.order_id)
        except Exception:  # noqa: BLE001 —— 订单源异常如实记 UNKNOWN，绝不伪造默认值
            pr = None
        if pr is not None and pr.ok and pr.order is not None:
            refundable = float(pr.order.refundable_amount_yuan or 0)
            if snap.gate_result == GATE_PASS and refundable <= 0:
                diffs.append(dict(
                    category=CAT_VERIFY, field="refundable_drift_zero",
                    severity=RECON_SEV_HIGH,
                    expected={"snapshot_refundable": True},
                    actual={"current_refundable": refundable},
                    diff_detail="快照判定可退，当前订单源显示无可退金额（漂移）",
                ))
            if REASON_NO_REFUNDABLE in (snap.reason_codes or []) and refundable > _PRICE_TOLERANCE:
                diffs.append(dict(
                    category=CAT_VERIFY, field="refundable_drift_recovered",
                    severity=RECON_SEV_INFO,
                    expected={"snapshot_refundable": False},
                    actual={"current_refundable": refundable},
                    diff_detail="快照判定无可退，当前订单源恢复可退（提示级漂移）",
                ))

    # ---- amount：申请金额 vs 订单可退金额 ----
    if case.requested_amount and float(case.requested_amount) > 0:
        try:
            pr = provider.lookup(case.order_id)
        except Exception:  # noqa: BLE001
            pr = None
        if pr is not None and pr.ok and pr.order is not None:
            refundable = float(pr.order.refundable_amount_yuan or 0)
            requested = float(case.requested_amount)
            if requested > refundable + _PRICE_TOLERANCE:
                diffs.append(dict(
                    category=CAT_AMOUNT, field="request_exceeds_refundable",
                    severity=RECON_SEV_HIGH,
                    expected={"requested_amount": requested, "refundable_lte": True},
                    actual={"refundable_amount": refundable},
                    diff_detail=f"申请金额 {requested} 超订单可退 {refundable}",
                ))
        else:
            if case.final_decision == DECISION_APPROVED:
                diffs.append(dict(
                    category=CAT_AMOUNT, field="approved_without_refundable_base",
                    severity=RECON_SEV_HIGH,
                    expected={"refundable_base": "存在"},
                    actual={"provider": "不可判定"},
                    diff_detail="已批准但订单源给不出可退基线（应安全挂起转人工）",
                ))

    # ---- refund_status：状态与回执轨迹一致 ----
    events = _refund_events_for(db, case.id)
    if case.refund_status == REFUND_STATUS_SUCCESS:
        if not any(e.new_status == REFUND_STATUS_SUCCESS for e in events):
            diffs.append(dict(
                category=CAT_REFUND_STATUS, field="success_without_receipt",
                severity=RECON_SEV_HIGH,
                expected={"event": "refund_event SUCCESS"},
                actual={"events": [e.event_type for e in events]},
                diff_detail="case 标记退款成功但退款事件流无 SUCCESS 回执",
            ))
    txn = _latest_transaction(db, case.id)
    if txn is not None and txn.status == "FAILED" and case.refund_status != "FAILED":
        diffs.append(dict(
            category=CAT_REFUND_STATUS, field="receipt_failed_out_of_sync",
            severity=RECON_SEV_HIGH,
            expected={"case.refund_status": "FAILED"},
            actual={"case.refund_status": case.refund_status, "txn_status": txn.status},
            diff_detail="退款回执 FAILED 但案件状态未同步为 FAILED",
        ))

    return diffs


# =====================================================================
# 对账运行（持久化）
# =====================================================================

def run_reconciliation(db: Session, *, run_id: str | None = None,
                       case_id: str | None = None,
                       window_minutes: int | None = None,
                       provider=None, actor_id: str | None = None) -> dict:
    """执行一轮对账并落 reconciliation_events（幂等，只增不改）。

    默认取近 `window_minutes`（RECONCILE_WINDOW_MINUTES）内更新的业务案件；
    传 case_id 则只对单案。同 run 同案同差异 guid 唯一 -> 重跑不重复落审计。
    """
    run_id = run_id or f"recon-{_now_naive().strftime('%Y%m%d%H%M%S')}"
    window = window_minutes or int(settings.RECONCILE_WINDOW_MINUTES)
    since = _now_naive() - timedelta(minutes=max(1, window))

    q = db.query(RefundCase)
    if case_id:
        q = q.filter(RefundCase.id == case_id)
    else:
        q = q.filter(RefundCase.updated_at >= since)
    cases = q.order_by(RefundCase.created_at).all()

    if provider is None:
        from app.infrastructure.order_provider import get_order_provider
        provider = get_order_provider()

    created_count = 0
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    new_alerts: list[str] = []
    examined = 0
    for case in cases:
        examined += 1
        for d in check_case(db, case=case, provider=provider):
            guid = _sha256_key(run_id, case.id, d["category"], d["field"])
            exists = db.query(ReconciliationEvent).filter_by(guid=guid).first()
            if exists:
                continue
            row = ReconciliationEvent(
                id=new_uuid(), case_id=case.id, run_id=run_id,
                category=d["category"], field=d["field"], severity=d["severity"],
                expected=d["expected"], actual=d["actual"],
                diff_detail=d["diff_detail"], guid=guid,
            )
            db.add(row)
            db.flush()
            created_count += 1
            by_severity[d["severity"]] = by_severity.get(d["severity"], 0) + 1
            by_category[d["category"]] = by_category.get(d["category"], 0) + 1
            if d["severity"] in (RECON_SEV_HIGH, RECON_SEV_CRITICAL):
                new_alerts.append(row.id)
    db.commit()

    if examined:
        from app.infrastructure.audit import write_event
        write_event(db, actor_id=actor_id, case_id=case_id,
                    action="RECONCILIATION_RUN",
                    payload={"run_id": run_id, "window_minutes": window,
                             "examined": examined, "created": created_count,
                             "by_severity": by_severity, "by_category": by_category})
        db.commit()

    return {
        "run_id": run_id,
        "window_minutes": window,
        "examined": examined,
        "created": created_count,
        "by_severity": by_severity,
        "by_category": by_category,
        "new_alerts": new_alerts,
    }


# =====================================================================
# 查询/处置
# =====================================================================

def list_diffs(db: Session, *, case_id: str | None = None,
               severity: str | None = None, unresolved_only: bool = False,
               limit: int = 50) -> list[dict]:
    q = db.query(ReconciliationEvent).order_by(ReconciliationEvent.created_at.desc())
    if case_id:
        q = q.filter(ReconciliationEvent.case_id == case_id)
    if severity:
        q = q.filter(ReconciliationEvent.severity == severity)
    if unresolved_only:
        q = q.filter(ReconciliationEvent.resolved.is_(False))
    rows = q.limit(limit).all()
    return [_recon_as_dict(r) for r in rows]


def overview(db: Session, *, window_minutes: int | None = None) -> dict:
    """对账大盘：近窗口差异统计 + 类别分布 + 未处置告警数 + 最近差异。"""
    window = window_minutes or int(settings.RECONCILE_WINDOW_MINUTES)
    since = _now_naive() - timedelta(minutes=max(1, window))
    rows = (db.query(ReconciliationEvent)
            .filter(ReconciliationEvent.created_at >= since).all())
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    unresolved = 0
    for r in rows:
        if r.severity in (RECON_SEV_HIGH, RECON_SEV_CRITICAL) and not r.resolved:
            unresolved += 1
        by_severity[r.severity] = by_severity.get(r.severity, 0) + 1
        by_category[r.category] = by_category.get(r.category, 0) + 1
    recent = list_diffs(db, limit=20)
    return {
        "window_minutes": window,
        "total": len(rows),
        "by_severity": by_severity,
        "by_category": by_category,
        "unresolved_alerts": unresolved,
        "recent": recent,
    }


def resolve_alert(db: Session, event_id: str, *, actor_id: str | None = None) -> dict | None:
    """主管处置差异（只置 resolved 标记，绝不删除审计行）。"""
    row = db.get(ReconciliationEvent, event_id)
    if row is None:
        return None
    if not row.resolved:
        row.resolved = True
        db.commit()
    return _recon_as_dict(row)


def _recon_as_dict(r: ReconciliationEvent) -> dict:
    return {
        "id": r.id, "case_id": r.case_id, "run_id": r.run_id,
        "category": r.category, "field": r.field, "severity": r.severity,
        "expected": r.expected, "actual": r.actual, "diff_detail": r.diff_detail,
        "guid": r.guid, "resolved": bool(r.resolved),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# =====================================================================
# 多来源判定留痕（OrderSourceVerdict，每来源一行，幂等去重）
# =====================================================================

def record_source_verdicts(db: Session, *, case: RefundCase,
                           multi_result: dict) -> int:
    """把一次多来源聚合的各来源判定落 OrderSourceVerdict（幂等：同案同源不重复）。"""
    from app.domain.models import OrderSourceVerdict
    recorded = 0
    for v in multi_result.get("sources") or []:
        exists = (db.query(OrderSourceVerdict)
                  .filter_by(case_id=case.id, source_id=v["source_id"])
                  .first())
        if exists:
            continue
        db.add(OrderSourceVerdict(
            id=new_uuid(), case_id=case.id, order_id=case.order_id,
            source_id=v["source_id"], trusted=bool(v["trusted"]),
            verdict=v["verdict"], reason=v.get("reason") and {"detail": v["reason"]},
            degraded=bool(v.get("degraded")),
        ))
        recorded += 1
    if recorded:
        db.commit()
    return recorded


def list_source_verdicts(db: Session, case_id: str, *, limit: int = 20) -> list[dict]:
    from app.domain.models import OrderSourceVerdict
    rows = (db.query(OrderSourceVerdict)
            .filter_by(case_id=case_id)
            .order_by(OrderSourceVerdict.created_at.desc())
            .limit(limit).all())
    return [{
        "id": r.id, "case_id": r.case_id, "order_id": r.order_id,
        "source_id": r.source_id, "trusted": bool(r.trusted),
        "verdict": r.verdict, "reason": r.reason, "degraded": bool(r.degraded),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


# =====================================================================
# 对账度量（时间窗聚合；只读）
# =====================================================================

def reconciliation_metrics(db: Session, *, window_minutes: int | None = None) -> dict:
    """核验失败率 / 不一致率 / 退款成功率与失败率/重试次数（近窗口）。

    数据源（只读）：
      - order_lookup_snapshots：核验 gate_result（核验失败率 = 非 PASS / 总查询）；
      - refund_events：OK/FAIL/RETRY/REPLAY（退款成功率 / 失败率 / 重试次数）；
      - reconciliation_events：差异（不一致率 = CRITICAL+HIGH / 案件数）。
    """
    window = window_minutes or int(settings.RECONCILE_WINDOW_MINUTES)
    since = _now_naive() - timedelta(minutes=max(1, window))

    from app.domain.models import OrderLookupSnapshot
    snaps = (db.query(OrderLookupSnapshot)
             .filter(OrderLookupSnapshot.created_at >= since).all())
    total_verify = len(snaps)
    verify_fail = sum(1 for s in snaps if s.gate_result != GATE_PASS)
    verify_fail_rate = round(verify_fail / total_verify, 4) if total_verify else None

    events = (db.query(RefundEvent)
              .filter(RefundEvent.created_at >= since).all())
    ok = sum(1 for e in events if e.event_type in ("OK", "EXEC") and e.new_status == REFUND_STATUS_SUCCESS)
    fail = sum(1 for e in events if e.event_type == "FAIL")
    retry = sum(1 for e in events if e.event_type == "RETRY")
    replay = sum(1 for e in events if e.event_type == "REPLAY")
    txn_total = ok + fail
    refund_success_rate = round(ok / txn_total, 4) if txn_total else None

    cases = db.query(RefundCase).filter(RefundCase.updated_at >= since).count() or 1
    recon_rows = (db.query(ReconciliationEvent)
                  .filter(ReconciliationEvent.created_at >= since).all())
    recon_high = sum(1 for r in recon_rows
                     if r.severity in (RECON_SEV_HIGH, RECON_SEV_CRITICAL))
    inconsist_rate = round(recon_high / cases, 4)

    return {
        "window_minutes": window,
        "verify_total": total_verify,
        "verify_fail_rate": verify_fail_rate,
        "refund_ok": ok,
        "refund_fail_rate": round(fail / txn_total, 4) if txn_total else None,
        "refund_retries": retry,
        "refund_replays": replay,
        "inconsistency_rate": inconsist_rate,
        "recon_high_events": recon_high,
    }