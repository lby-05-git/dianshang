# -*- coding: utf-8 -*-
"""Phase 7 验证：幂等结果缓存 / 审批分布式锁（并发 409、锁超时恢复）/ 退款防重。

并发审批语义（docs/07 手册 ）：
- 两主管同时审批同一案件 -> 只有一个成功，另一个 409；
- 锁带 TTL，持有者崩溃后自动过期可重试；
- 锁释放用 Lua 校验 token，不误删他人锁。
"""
import time
import uuid

from app.domain.models import (
    CASE_STATUS_SUSPENDED,
    DECISION_PENDING,
    HumanIntervention,
    RefundCase,
)
from app.infrastructure.redis_client import redis_client
from tests.conftest import SessionTest


def _make_suspended_case() -> str:
    s = SessionTest()
    from app.domain.models import User
    u = User(username=f"lk_{uuid.uuid4().hex[:8]}", password_hash="x", role="supervisor")
    s.add(u)
    s.flush()
    c = RefundCase(order_id=f"ORD-{uuid.uuid4().hex[:8]}", requested_amount=350,
                   submitter_id=u.id, current_status=CASE_STATUS_SUSPENDED,
                   final_decision=DECISION_PENDING)
    s.add(c)
    s.commit()
    s.close()
    return c.id


def _approve(client, token: str, cid: str, idem: str):
    return client.post(
        f"/api/v1/refunds/{cid}/approve",
        data={"opinion": "批准"},
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idem},
    )


def _login(client, username: str) -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": "123456"})
    assert resp.status_code == 200
    return resp.json()["token"]


def test_approve_same_idempotency_key_returns_cached(client):
    cid = _make_suspended_case()
    sup = _login(client, "supervisor1")
    idem = f"ap-{uuid.uuid4().hex}"
    r1 = _approve(client, sup, cid, idem)
    assert r1.status_code == 202
    r2 = _approve(client, sup, cid, idem)  # 网络重试场景：同键
    assert r2.status_code == 202
    assert r2.json()["case_id"] == r1.json()["case_id"]
    assert r2.json()["action"] == "APPROVED"  # 返回缓存结果而非 409


def test_concurrent_approval_lock_conflict(client):
    """锁被占用（另一主管正在审批）-> 409。"""
    cid = _make_suspended_case()
    sup = _login(client, "supervisor1")
    redis_client.set(f"lock:approve:{cid}", "other-request", nx=True, ex=10)
    r = _approve(client, sup, cid, f"ap-{uuid.uuid4().hex}")
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"


def test_lock_expiry_allows_retry(client):
    """锁超时（持有者崩溃）-> 过期后恢复可审批。"""
    cid = _make_suspended_case()
    sup = _login(client, "supervisor1")
    redis_client.set(f"lock:approve:{cid}", "crashed-holder", nx=True, ex=1)
    time.sleep(1.2)
    r = _approve(client, sup, cid, f"ap-{uuid.uuid4().hex}")
    assert r.status_code == 202


def test_lock_release_with_wrong_token_keeps_lock():
    """Lua 释放：他人 token 无法删除锁（防误删，手册 坑2）。"""
    key = f"lock:approve:{uuid.uuid4().hex}"
    redis_client.set(key, "owner-a", nx=True, ex=10)
    redis_client.eval(
        "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
        1, key, "owner-b",
    )
    assert redis_client.get(key) == "owner-a"  # 锁仍在


def test_refund_idempotent_no_double_execution():
    """退款幂等：同案件重复执行只产生一个流水号。"""
    from app.infrastructure.refund import MockRefundProvider

    cid = f"rf-{uuid.uuid4().hex}"
    p = MockRefundProvider()
    ok1, _ = p.execute(cid, 128)
    ok2, _ = p.execute(cid, 128)  # Worker 重投场景
    assert ok1 and ok2
    assert p.last_refund_id is not None
    # Redis 中只有一条退款流水记录
    assert redis_client.get(f"refund:{cid}") == p.last_refund_id


def test_approve_different_keys_same_case_conflict(client):
    """不同幂等键并发审批同一案件 -> 第二个 409（干预记录防线）。"""
    cid = _make_suspended_case()
    sup = _login(client, "supervisor1")
    r1 = _approve(client, sup, cid, f"ap-{uuid.uuid4().hex}")
    assert r1.status_code == 202
    r2 = _approve(client, sup, cid, f"ap-{uuid.uuid4().hex}")
    assert r2.status_code == 409
