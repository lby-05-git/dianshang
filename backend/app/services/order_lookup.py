# -*- coding: utf-8 -*-
"""订单中心沙箱查询快照（+ 商品/价格一致 + 可退资格）。

基于既有 `order_provider` 的确定性订单源，把每次查询结果落一条不可变快照到
`order_lookup_snapshots`（add-only，绝不改写审计轨迹）。供详情页/审计按
`case_id` 追溯每笔核验查到了什么——存在性/归属/交易状态/可退资格都要能复盘。

设计要点
--------
- **只读佐证，不改业务语义**：本服务只产生快照，不参与判定、不执行退款。
- **超时/不可用 → 安全挂起**：Provider 超时/网络错如实落 UNKNOWN + reason_code
  `ORDER_LOOKUP_TIMEOUT`，绝不伪造默认值当真实（与 order_provider 红线一致）。
- **raw_sha256**：快照原文（订单字段）哈希，防他人篡改审计内容（存哈希不存明文）。
- **reason_codes 结构化原因码**：供前端/监控机器可读；空表示本次查询正常。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.domain.models import (
    OrderLookupSnapshot,
    RefundCase,
    new_uuid,
)
from app.infrastructure.order_provider import (
    MATCH,
    MISMATCH,
    UNKNOWN,
    ProviderResult,
    verify_order,
)

GATE_RESULT_PASS = "PASS"
GATE_RESULT_BLOCK = "BLOCK"
GATE_RESULT_SUSPEND = "SUSPEND"      # 查询异常 -> 安全挂起（不直接 BLOCK 拒绝）

LOOKUP_KIND_API = "api"               # 详情页 order-verify 端点（P11 之后人工复核）
LOOKUP_KIND_PROCESS = "process"       # 图内核验节点（P10）

REASON_ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
REASON_OWNER_MISMATCH = "OWNER_MISMATCH"
REASON_TRADE_STATUS = "TRADE_STATUS"
REASON_NO_REFUNDABLE = "NO_REFUNDABLE"
REASON_LOOKUP_TIMEOUT = "ORDER_LOOKUP_TIMEOUT"
REASON_PROVIDER_UNAVAILABLE = "ORDER_PROVIDER_UNAVAILABLE"


def _sha256(blob: dict) -> str:
    canon = json.dumps(blob, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def snapshot_order_lookup(
    db: Session,
    *,
    case: RefundCase,
    provider=None,
    lookup_kind: str = LOOKUP_KIND_API,
) -> OrderLookupSnapshot:
    """对指定案件做一次订单中心沙箱查询并落不可变快照。

    返回 `OrderLookupSnapshot`（已 commit）。异常（超时/不可用）返回快照且
    gate_result 标 `SUSPEND`；订单不存在/归属不符/状态异常/无可退金额标 `BLOCK`。
    """
    if provider is None:
        from app.infrastructure.order_provider import get_order_provider

        provider = get_order_provider()

    # 归属人：用案件提交人 username（order_provider.verify_order 入参）
    requester_username = _submission_username(db, case)

    try:
        pr: ProviderResult = provider.lookup(case.order_id)
    except Exception:  # noqa: BLE001 —— provider 抛异常也按不可用处理
        pr = ProviderResult(ok=False, error=f"订单源不可用: {case.order_id}",
                            available=False, timed_out=True)

    if not pr.ok:
        exists = False
        gate_result = GATE_RESULT_SUSPEND if (pr.timed_out or not pr.available) else GATE_RESULT_BLOCK
        reason_codes = []
        if pr.timed_out or not pr.available:
            reason_codes.append(REASON_LOOKUP_TIMEOUT if pr.timed_out else REASON_PROVIDER_UNAVAILABLE)
        else:
            reason_codes.append(REASON_ORDER_NOT_FOUND)
        order_blob = {"error": pr.error}
        return _commit_snapshot(
            db, case=case, order=order_blob, lookup_kind=lookup_kind,
            exists=exists, owner_locked=False, trade_status=None, refundable=False,
            gate_result=gate_result, reason_codes=reason_codes,
        )

    order = pr.order
    verify = verify_order(provider, case.order_id, requester_username)
    verdict = verify["order_verification"]
    owner_locked = (verdict == MATCH)          # 归属一致 = 主人核验锁定
    refundable = verify.get("refundable_amount_yuan", 0) is not None and \
        verify.get("refundable_amount_yuan", 0) > 0
    reason_codes = []
    if verdict == MISMATCH:
        if REASON_ORDER_NOT_FOUND in verify.get("reasons", []):
            reason_codes.append(REASON_ORDER_NOT_FOUND)
        if any("归属" in r for r in verify.get("reasons", [])):
            reason_codes.append(REASON_OWNER_MISMATCH)
        if any("交易状态" in r for r in verify.get("reasons", [])):
            reason_codes.append(REASON_TRADE_STATUS)
        if not refundable:
            reason_codes.append(REASON_NO_REFUNDABLE)
    elif verdict == UNKNOWN:
        reason_codes.append(REASON_LOOKUP_TIMEOUT)

    gate_result = (GATE_RESULT_SUSPEND if verdict == UNKNOWN
                   else GATE_RESULT_BLOCK if verdict == MISMATCH
                   else GATE_RESULT_PASS)
    order_blob = {
        "order_id": order.order_id,
        "order_no": order.order_no,
        "trade_status": order.status,
        "total_price_yuan": order.total_price_yuan,
        "paid_amount_yuan": order.paid_amount_yuan,
        "refunded_amount_yuan": order.refunded_amount_yuan,
        "refundable_amount_yuan": order.refundable_amount_yuan,
        "items": [
            {"sku": it.sku, "name": it.name, "spec": it.spec, "quantity": it.quantity}
            for it in order.items
        ],
    }

    return _commit_snapshot(
        db, case=case, order=order_blob, lookup_kind=lookup_kind,
        exists=True, owner_locked=owner_locked, trade_status=order.status,
        refundable=refundable, gate_result=gate_result,
        reason_codes=reason_codes or None,
    )


def _submission_username(db: Session, case: RefundCase) -> str:
    from app.domain.models import User

    if case.submitter_id:
        u = db.get(User, case.submitter_id)
        if u is not None:
            return u.username
    return "unknown"


def _commit_snapshot(
    db: Session,
    *,
    case: RefundCase,
    order: dict,
    lookup_kind: str,
    exists: bool,
    owner_locked: bool,
    trade_status: str | None,
    refundable: bool,
    gate_result: str,
    reason_codes: list[str] | None,
) -> OrderLookupSnapshot:
    """构造快照行 + 更新案件 last_order_snapshot_id；单事务提交。"""
    snap = OrderLookupSnapshot(
        id=new_uuid(),
        case_id=case.id,
        order_id=case.order_id,
        exists=exists,
        owner_locked=owner_locked,
        trade_status=trade_status,
        refundable=refundable,
        lookup_kind=lookup_kind,
        gate_result=gate_result,
        reason_codes=reason_codes,
        raw_sha256=_sha256({"order": order, "lookup_kind": lookup_kind}),
    )
    db.add(snap)
    db.flush()
    case.last_order_snapshot_id = snap.id
    db.commit()
    return snap


def latest_snapshot(db: Session, case_id: str) -> OrderLookupSnapshot | None:
    """案件最近一次订单快照（供详情页/审计）。"""
    return (
        db.query(OrderLookupSnapshot)
        .filter_by(case_id=case_id)
        .order_by(OrderLookupSnapshot.created_at.desc())
        .first()
    )


def list_snapshots(db: Session, case_id: str, *, limit: int = 20) -> list[OrderLookupSnapshot]:
    return (
        db.query(OrderLookupSnapshot)
        .filter_by(case_id=case_id)
        .order_by(OrderLookupSnapshot.created_at.desc())
        .limit(limit)
        .all()
    )