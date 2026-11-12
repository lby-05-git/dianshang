# -*- coding: utf-8 -*-
"""Phase 2 验证：模型落库回环、唯一约束、状态乐观锁语义（WHERE 条件更新）。"""
import pytest
from decimal import Decimal
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.models import (
    CASE_STATUS_SUSPENDED,
    DECISION_PENDING,
    AgentExecutionLog,
    Evidence,
    HumanIntervention,
    RefundCase,
    SystemConfig,
    User,
)
from app.infrastructure.db import SessionLocal
from app.infrastructure.seed import DEFAULT_CONFIGS, seed


@pytest.fixture()
def db():
    seed()  # 幂等种子
    session = SessionLocal()
    yield session
    session.close()


def test_roundtrip_full_chain(db):
    user = User(username=f"t_user_{__import__('uuid').uuid4().hex[:8]}",
                password_hash="x", role="customer_service")
    db.add(user)
    db.flush()
    case = RefundCase(order_id="ORD-1001", requested_amount=Decimal("350.00"),
                      submitter_id=user.id, current_status=CASE_STATUS_SUSPENDED,
                      final_decision=DECISION_PENDING)
    db.add(case)
    db.flush()
    db.add(Evidence(case_id=case.id, file_url="/uploads/a.jpg"))
    db.add(AgentExecutionLog(case_id=case.id, agent_name="intake", status="SUCCESS",
                             step_order=1))
    db.add(HumanIntervention(case_id=case.id, approver_id=user.id, action="APPROVED",
                             opinion="情况属实"))
    db.commit()

    got = db.query(RefundCase).filter_by(id=case.id).one()
    assert got.requested_amount == Decimal("350.00")
    assert got.current_status == CASE_STATUS_SUSPENDED
    assert len(got_id := db.query(Evidence).filter_by(case_id=case.id).all()) == 1


def test_unique_username(db):
    name = f"dup_{__import__('uuid').uuid4().hex[:8]}"
    db.add(User(username=name, password_hash="x", role="customer_service"))
    db.commit()
    db.add(User(username=name, password_hash="y", role="supervisor"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_optimistic_lock_state_condition(db):
    """乐观锁语义（docs/06 E-13）：只有状态仍为 SUSPENDED 才能审批成功。"""
    user = User(username=f"lock_{__import__('uuid').uuid4().hex[:8]}",
                password_hash="x", role="supervisor")
    db.add(user)
    db.flush()
    case = RefundCase(order_id="ORD-LOCK", requested_amount=Decimal("128.00"),
                      submitter_id=user.id, current_status=CASE_STATUS_SUSPENDED,
                      final_decision=DECISION_PENDING)
    db.add(case)
    db.commit()

    # 第一次审批：状态条件命中 -> 1 行
    r1 = db.execute(
        text("UPDATE refund_cases SET current_status='COMPLETED', final_decision='APPROVED' "
             "WHERE id=:cid AND current_status='SUSPENDED'"),
        {"cid": case.id},
    )
    db.commit()
    assert r1.rowcount == 1

    # 第二次审批（并发场景）：状态已是 COMPLETED -> 0 行 -> 应判 409
    r2 = db.execute(
        text("UPDATE refund_cases SET current_status='COMPLETED', final_decision='APPROVED' "
             "WHERE id=:cid AND current_status='SUSPENDED'"),
        {"cid": case.id},
    )
    db.commit()
    assert r2.rowcount == 0


def test_system_configs_seeded(db):
    keys = {row[0] for row in db.query(SystemConfig.cfg_key).all()}
    assert {k for k, _, _ in DEFAULT_CONFIGS} <= keys
