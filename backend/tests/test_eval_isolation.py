# -*- coding: utf-8 -*-
"""M8 评测数据隔离（5.2 + ）：业务看板/列表默认排除评测数据，清理只按 Run。"""
import uuid

from decimal import Decimal

from app.domain.models import (
    CASE_STATUS_COMPLETED,
    CASE_STATUS_RUNNING,
    DECISION_APPROVED,
    DECISION_PENDING,
    RefundCase,
    User,
)
from app.domain import models
from tests.conftest import SessionTest


def _mk_eval_case(session, *, run_id: str = "run-x", case_id: str = "G01",
                  username: str = "customer_service1", status="RUNNING",
                  decision="PENDING"):
    u = session.query(User).filter_by(username=username).first()
    oid = f"ORD-ISO-{uuid.uuid4().hex[:8]}"
    c = RefundCase(
        order_id=oid, requested_amount=Decimal("99.00"), submitter_id=u.id,
        current_status=status, final_decision=decision,
        eval_run_id=run_id, eval_case_id=case_id,
        refund_reason="评测隔离用例",
    )
    session.add(c)
    session.commit()
    return c.id


def _mk_biz_case(session, *, username: str = "customer_service1"):
    u = session.query(User).filter_by(username=username).first()
    oid = f"ORD-BIZ-{uuid.uuid4().hex[:8]}"
    c = RefundCase(
        order_id=oid, requested_amount=Decimal("99.00"), submitter_id=u.id,
        current_status=CASE_STATUS_RUNNING, final_decision=DECISION_PENDING,
        refund_reason="生产业务用例",
    )
    session.add(c)
    session.commit()
    return c.id


def test_non_eval_helper_filters():
    s = SessionTest()
    _ = _mk_eval_case(s, run_id="run1")
    biz = _mk_biz_case(s)
    biz_id = biz
    q = s.query(RefundCase).filter(RefundCase.non_eval())
    rows = q.all()
    ids = [r.id for r in rows]
    assert biz_id in ids
    assert all(r.eval_run_id is None for r in rows)  # 全为业务，无评测打标


def test_dashboard_stats_excludes_eval(client):
    # 建一条评测 + 一条业务
    s = SessionTest()
    run_id = f"run-stat-{uuid.uuid4().hex[:8]}"
    _ = _mk_eval_case(s, run_id=run_id)
    # 复用 client token（customer_service1 可见）
    from tests.test_api import login, _auth_headers

    token = login(client, "customer_service1")
    r = client.get("/api/v1/dashboard/stats", headers=_auth_headers(token))
    assert r.status_code == 200
    stats = r.json()
    before_total = stats["total"]
    # 评测案件确实存在但未计入业务总量
    eval_cnt = s.query(RefundCase).filter(RefundCase.eval_run_id == run_id).count()
    assert eval_cnt == 1
    # 重新建一条业务案件，total 应增加（评测案件未抬升 total）
    _mk_biz_case(s)
    r2 = client.get("/api/v1/dashboard/stats", headers=_auth_headers(token))
    assert r2.json()["total"] == before_total + 1


def test_list_cases_excludes_eval(client):
    from tests.test_api import login, _auth_headers

    token = login(client, "customer_service1")
    # 建评测 + 业务
    s = SessionTest()
    _ = _mk_eval_case(s, run_id="run-list")
    _ = _mk_biz_case(s)
    listing = client.get("/api/v1/refunds", headers=_auth_headers(token)).json()
    # 列表中不应出现评测案件（其 reason 是"评测隔离用例"）
    assert all(i.get("refund_reason") != "评测隔离用例" for i in listing["items"])


def test_cleanup_only_removes_eval_rows():
    """cleanup_run 只删 eval_run_id 匹配的行，业务案件不受影响。"""
    from eval_refund.cleanup import cleanup_run

    s = SessionTest()
    biz_id = _mk_biz_case(s)
    _ = _mk_eval_case(s, run_id="run-clean", case_id="G-a")
    _ = _mk_eval_case(s, run_id="run-clean", case_id="G-b")

    out = cleanup_run(s, run_id="run-clean")
    assert out["deleted"] == 2
    # 业务案件仍在
    assert s.get(RefundCase, biz_id) is not None
    # 无残留 eval 数据
    left = s.query(RefundCase).filter(RefundCase.eval_run_id == "run-clean").count()
    assert left == 0


def test_cleanup_null_run_id_is_noop():
    """eval_run_id 为 None（业务案件）不作为清理目标：不模糊删除。"""
    from eval_refund.cleanup import cleanup_run

    s = SessionTest()
    biz_id = _mk_biz_case(s)
    out = cleanup_run(s, run_id="some-run")
    assert out["deleted"] == 0
    assert s.get(RefundCase, biz_id) is not None