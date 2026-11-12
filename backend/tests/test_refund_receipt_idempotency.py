# -*- coding: utf-8 -*-
"""补③退款回执重复追加防护（幂等/审计）。

覆盖：
1. 首次执行 -> 落一条 SUCCESS 回执；
2. 幂等 skip（幂等键已存在，二次 execute）-> 只更新既有回执，绝不重复 INSERT；
3. 失败注入（无幂等键）-> 落一条 FAILED 回执；
4. 已成功后退款失败注入 -> 不覆盖到账状态（状态机不回滚）。

每次读写都开独立会话、用完即关，不与 provider 内部会话交错事务，避免 MySQL 行锁竞争。
"""
import uuid
from decimal import Decimal

from app.domain.models import (
    CASE_STATUS_COMPLETED,
    DECISION_APPROVED,
    REFUND_STATUS_FAILED,
    REFUND_STATUS_SUCCESS,
    RefundCase,
    RefundTransaction,
    User,
)
from app.infrastructure.redis_client import redis_client
from tests.conftest import SessionTest

# 备注：SessionLocal 注入测试库，保证 _record 落库到测试库（同 workflow 测试）
from app.infrastructure import refund
_refund_module = refund


def _create_case() -> str:
    """独立会话建 case，commit 后关闭。返回 case_id。"""
    db = SessionTest()
    try:
        u = db.query(User).filter_by(username="customer_service1").first()
        if u is None:
            u = User(username=f"rf_{uuid.uuid4().hex[:8]}", password_hash="x",
                     role="customer_service")
            db.add(u)
            db.flush()
        c = RefundCase(order_id=f"ORD-RF-{uuid.uuid4().hex[:8]}",
                       requested_amount=Decimal("88.00"),
                       submitter_id=u.id,
                       current_status=CASE_STATUS_COMPLETED,
                       final_decision=DECISION_APPROVED)
        db.add(c)
        db.flush()
        cid = c.id
        db.commit()
        return cid
    finally:
        db.close()


def _receipts(cid: str) -> list[dict]:
    """独立只读会话查询回执，即查即关。"""
    db = SessionTest()
    try:
        rows = (db.query(RefundTransaction)
                .filter_by(case_id=cid)
                .order_by(RefundTransaction.created_at)
                .all())
        return [{"id": r.id, "refund_id": r.refund_id, "status": r.status,
                 "amount": r.amount, "error": r.error} for r in rows]
    finally:
        db.close()


def _case_refund_status(cid: str) -> str | None:
    db = SessionTest()
    try:
        c = db.get(RefundCase, cid)
        return c.refund_status if c else None
    finally:
        db.close()


def _cleanup(cid: str) -> None:
    try:
        redis_client.delete(f"refund:{cid}")
    except Exception:  # noqa: BLE001 —— 清理非关键
        pass
    db = SessionTest()
    try:
        for r in db.query(RefundTransaction).filter_by(case_id=cid).all():
            db.delete(r)
        db.commit()
    except Exception:  # noqa: BLE001 —— 清理非关键
        db.rollback()
    finally:
        db.close()


def test_refund_receipt_single_on_success(monkeypatch):
    """首次执行落一条 SUCCESS 回执；幂等 skip 不重复 INSERT。"""
    from app.infrastructure import refund as rf
    monkeypatch.setattr(rf, "SessionLocal", SessionTest)

    cid = _create_case()
    try:
        p = rf.MockRefundProvider()
        ok, err = p.execute(cid, Decimal("88.00"))
        assert ok and err is None
        rx = _receipts(cid)
        assert len(rx) == 1 and rx[0]["status"] == REFUND_STATUS_SUCCESS

        # 幂等 skip：同 case 二次执行，仍只有一条回执（更新，不追加）
        ok2, err2 = p.execute(cid, Decimal("88.00"))
        assert ok2 and err2 is None
        rx2 = _receipts(cid)
        assert len(rx2) == 1
        assert rx2[0]["status"] == REFUND_STATUS_SUCCESS
        assert rx2[0]["refund_id"] == rx[0]["refund_id"]
        assert _case_refund_status(cid) == REFUND_STATUS_SUCCESS
    finally:
        _cleanup(cid)


def test_refund_failure_records_failed_then_retry_success_appends(monkeypatch):
    """失败落 FAILED；重试成功后补 SUCCESS——两条并存，审计完整不重复。"""
    from app.infrastructure import refund as rf
    monkeypatch.setattr(rf, "SessionLocal", SessionTest)

    cid = _create_case()
    try:
        _orig = rf.MockRefundProvider

        # 注入失败：网关超时，且阻断幂等键占位（只为产生失败回执）
        p = _orig(fail_once=True)
        with monkeypatch.context() as m:
            m.setattr(rf.redis_client, "get", lambda k, *a, **kw: None)
            m.setattr(rf.redis_client, "set", lambda *a, **kw: None)
            ok, err = p.execute(cid, Decimal("88.00"))
        assert not ok and err is not None
        rx = _receipts(cid)
        assert len(rx) == 1 and rx[0]["status"] == REFUND_STATUS_FAILED

        # 重试成功 -> 补一条 SUCCESS，FAILED 与 SUCCESS 并存（审计完整）
        p2 = _orig()
        ok2, err2 = p2.execute(cid, Decimal("88.00"))
        assert ok2 and err2 is None
        rx2 = _receipts(cid)
        statuses = sorted(r["status"] for r in rx2)
        assert statuses == [REFUND_STATUS_FAILED, REFUND_STATUS_SUCCESS]
        # SUCCESS 回执有独立流水号，且与 FAILED 回执不同（同秒写入 created_at 排序不稳，
        # 按 status 定位而非 [-1]）
        fail_tx = next(r for r in rx2 if r["status"] == REFUND_STATUS_FAILED)
        succ_tx = next(r for r in rx2 if r["status"] == REFUND_STATUS_SUCCESS)
        assert succ_tx["refund_id"] and succ_tx["refund_id"] != fail_tx["refund_id"]
    finally:
        _cleanup(cid)


def test_refund_failure_after_success_keeps_success_state(monkeypatch):
    """已成功到账后，后续失败注入不覆盖为 FAILED（状态机不回滚）。"""
    from app.infrastructure import refund as rf
    monkeypatch.setattr(rf, "SessionLocal", SessionTest)

    cid = _create_case()
    try:
        p = rf.MockRefundProvider()
        assert p.execute(cid, Decimal("88.00")) == (True, None)

        # 幂等键已存在前提下 fail_once 应被跳过（不覆盖到账状态）
        p2 = rf.MockRefundProvider(fail_once=True)
        ok, err = p2.execute(cid, Decimal("88.00"))
        assert ok and err is None
        assert _case_refund_status(cid) == REFUND_STATUS_SUCCESS
        # 回执仍只有一条 SUCCESS（幂等 skip 未追加）
        rx = _receipts(cid)
        assert len(rx) == 1 and rx[0]["status"] == REFUND_STATUS_SUCCESS
    finally:
        _cleanup(cid)