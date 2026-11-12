# -*- coding: utf-8 -*-
"""死信服务单测（V2.0 -4 / 计划 test_dead_letter.py）。

覆盖：可查 / 修复重开 / 退款类人工重放幂等（replay_guid 不重复退款）/
非 refund 无 handler 拒绝（诚实不假装成功）/ handler 只驱一次 / 并发领取原子性 / 端点权限。
"""
import uuid
from decimal import Decimal

from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login

from app.domain.models import (
    CASE_STATUS_RUNNING,
    DECISION_PENDING,
    RefundCase,
    User,
)
from app.services import dead_letter as dl
from app.services import refund_events


def _make_case() -> str:
    s = SessionTest()
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x",
                 role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(order_id=f"ORD-DL-{uuid.uuid4().hex[:12]}",
                   requested_amount=Decimal("99.00"),
                   submitter_id=u.id,
                   current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING,
                   refund_reason="")
    s.add(c)
    s.flush()
    cid = c.id
    s.commit()
    s.close()
    return cid


# ---------- 查询 / 修复 ----------

def test_enqueue_list_get_repair_reopen():
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_REFUND,
                                  payload={"case_id": "x"}, error="timeout")
    listed = dl.list_dead_letters(s, status=dl.STATUS_OPEN)
    assert any(t["id"] == task.id and t["task_type"] == "refund" for t in listed)

    got = dl.get_dead_letter(s, task.id)
    assert got["status"] == "OPEN" and got["error"] == "timeout"

    # 修复：payload 修正 + 意图明确；FAILED 可重新 OPEN
    repaired = dl.repair_dead_letter(s, task.id, payload={"case_id": "y"},
                                     error="fixed")
    assert repaired["payload"] == {"case_id": "y"}
    assert repaired["error"] == "fixed"

    dl.replay_dead_letter(s, task.id, replayed_by="sup")  # 缺 case_id -> FAILED
    assert dl.get_dead_letter(s, task.id)["status"] == dl.STATUS_FAILED
    reopened = dl.repair_dead_letter(s, task.id, reopen=True,
                                     payload={"case_id": _make_case()})
    assert reopened["status"] == dl.STATUS_OPEN
    s.close()


def test_list_rejects_illegal_status():
    s = SessionTest()
    try:
        dl.list_dead_letters(s, status="BOGUS")
        assert False, "非法状态筛选应抛 ValueError"
    except ValueError:
        pass
    s.close()


def test_get_missing_raises():
    s = SessionTest()
    try:
        dl.get_dead_letter(s, "dl-not-exist")
        assert False, "不存在应抛 KeyError"
    except KeyError:
        pass
    s.close()


# ---------- 退款类重放：幂等不重复退款 ----------

def test_replay_refund_idempotent_no_duplicate():
    cid = _make_case()
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_REFUND,
                                  payload={"case_id": cid}, error="gate timeout")
    out1 = dl.replay_dead_letter(s, task.id, replayed_by="supervisor1")
    assert out1["replayed"] is True
    guid = out1["replay_guid"]

    # 事件流里只应有唯一一条该 guid（幂等键），绝不产生第二笔退款/重复事件
    def _count_guid():
        st = refund_events.query_refund_status(s, cid)
        return sum(1 for e in st["events"] if e["guid"] == guid)

    assert _count_guid() == 1

    # 再次重放：幂等跳过 already_closed，guid 不变，事件数仍 1
    out2 = dl.replay_dead_letter(s, task.id, replayed_by="supervisor1")
    assert out2["replayed"] is False
    assert out2["skipped"] == "already_closed"
    assert out2["replay_guid"] == guid
    assert _count_guid() == 1
    assert dl.get_dead_letter(s, task.id)["replayed_ok"] is True
    s.close()


def test_replay_refund_missing_case_id_fails_then_repairable():
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_REFUND, payload={})
    out = dl.replay_dead_letter(s, task.id, replayed_by="system")
    assert out["skipped"] == "missing_case_id"
    assert dl.get_dead_letter(s, task.id)["status"] == dl.STATUS_FAILED

    # 修复补 case_id 后可重放成功
    cid = _make_case()
    dl.repair_dead_letter(s, task.id, payload={"case_id": cid}, reopen=True)
    out2 = dl.replay_dead_letter(s, task.id, replayed_by="system")
    assert out2["replayed"] is True
    s.close()


def test_replay_non_refund_without_handler_rejected():
    """非 refund 无 handler：拒绝重放（绝不假装成功），置 FAILED。"""
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_INTENT, payload={})
    try:
        dl.replay_dead_letter(s, task.id)
        assert False, "非 refund 无 handler 应抛 ValueError"
    except ValueError:
        pass
    assert dl.get_dead_letter(s, task.id)["status"] == dl.STATUS_FAILED
    s.close()


def test_handler_replayed_once_and_never_twice():
    """handler 重驱：首轮恰好一次，CLOSED 后再重放不再触发（不重复执行）。"""
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_INTENT,
                                  payload={"task": "rebuild_index"})
    driven: list[str] = []

    def handler(db, t):
        driven.append(t["id"])

    out1 = dl.replay_dead_letter(s, task.id, handler=handler)
    assert out1["replayed"] is True
    assert len(driven) == 1

    out2 = dl.replay_dead_letter(s, task.id, handler=handler)
    assert out2["replayed"] is False and out2["skipped"] == "already_closed"
    assert len(driven) == 1               # 绝不多驱
    s.close()


def test_handler_failure_leaves_failed():
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_EVAL, payload={})

    def bad_handler(db, t):
        raise RuntimeError("worker crashed")

    out = dl.replay_dead_letter(s, task.id, handler=bad_handler)
    assert out["replayed"] is False
    assert dl.get_dead_letter(s, task.id)["status"] == dl.STATUS_FAILED
    s.close()


def test_concurrent_claim_only_one_wins():
    """OPEN->REPLAYING 条件 UPDATE：第二个领取者（假并发）拿不到。"""
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_REFUND,
                                  payload={"case_id": _make_case()})
    guid1 = dl.claim_for_replay(s, task.id)
    guid2 = dl.claim_for_replay(s, task.id)
    assert guid1 is not None
    assert guid2 is None                  # 已 REPLAYING，第二个 Worker 幂等跳过
    assert dl.get_dead_letter(s, task.id)["status"] == dl.STATUS_REPLAYING
    s.close()


# ---------- 端点（权限 + 幂等） ----------

def test_dead_letter_endpoints_permission_and_idempotent(client):
    cid = _make_case()
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_REFUND,
                                  payload={"case_id": cid})
    task_id = task.id
    s.close()

    # 客服无权（死信属安全运营面）
    cs_tok = login(client, "customer_service1")
    assert client.get("/api/v1/dead-letter",
                      headers=_auth_headers(cs_tok)).status_code == 403
    assert client.post(f"/api/v1/dead-letter/{task_id}/replay",
                       headers=_auth_headers(cs_tok)).status_code == 403

    sup_tok = login(client, "supervisor1")
    # 列表包含该任务
    listed = client.get("/api/v1/dead-letter",
                        headers=_auth_headers(sup_tok)).json()["items"]
    assert any(t["id"] == task_id for t in listed)

    # 重放成功且二次幂等跳过（不重复退款）
    r1 = client.post(f"/api/v1/dead-letter/{task_id}/replay",
                     headers=_auth_headers(sup_tok))
    assert r1.status_code == 200
    guid = r1.json()["replay_guid"]
    r2 = client.post(f"/api/v1/dead-letter/{task_id}/replay",
                     headers=_auth_headers(sup_tok))
    assert r2.status_code == 200
    assert r2.json()["skipped"] == "already_closed"
    assert r2.json()["replay_guid"] == guid

    # 404 / 400
    assert client.get("/api/v1/dead-letter/not-exist",
                      headers=_auth_headers(sup_tok)).status_code == 404
    s2 = SessionTest()
    itask = dl.enqueue_dead_letter(s2, task_type=dl.TASK_INTENT, payload={})
    iid = itask.id
    s2.close()
    assert client.post(f"/api/v1/dead-letter/{iid}/replay",
                       headers=_auth_headers(sup_tok)).status_code == 400


def test_dead_letter_repair_endpoint(client):
    s = SessionTest()
    task = dl.enqueue_dead_letter(s, task_type=dl.TASK_REFUND, payload={})
    task_id = task.id
    s.close()
    sup_tok = login(client, "supervisor1")
    resp = client.put(f"/api/v1/dead-letter/{task_id}/repair",
                      headers=_auth_headers(sup_tok),
                      json={"error": "修复说明", "payload": {"case_id": _make_case()}})
    assert resp.status_code == 200
    assert resp.json()["error"] == "修复说明"