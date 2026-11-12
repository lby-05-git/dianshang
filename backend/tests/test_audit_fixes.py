# -*- coding: utf-8 -*-
"""任务四审计修复的测试证明（docs/AI生成代码安全与性能审计报告）：
1. 登录限流 Redis 化（多 worker 共享，防爆破不被稀释）
2. human_interventions UNIQUE(case_id) 约束（并发双审批 DB 层兜底）
3. _get_config 30s 缓存（每案件 DB 往返 20 次 -> 1 次）
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.models import HumanIntervention, RefundCase, User
from app.infrastructure.redis_client import redis_client
from tests.conftest import SessionTest


# ---- 1. 登录限流：Redis 原子计数，多 worker 共享 ----
def _wrong_login(client, times: int):
    for _ in range(times):
        resp = client.post("/api/v1/auth/login",
                           json={"username": "supervisor1", "password": "wrong"})
        assert resp.status_code == 401


def test_rate_limit_returns_429_after_10_failures(client):
    _wrong_login(client, 10)
    resp = client.post("/api/v1/auth/login",
                       json={"username": "supervisor1", "password": "wrong"})
    assert resp.status_code == 429  # 第 11 次失败被限流


def test_rate_limit_failure_key_exists_in_redis(client):
    _wrong_login(client, 3)
    keys = redis_client.keys("login:fail:*")
    assert keys, "失败尝试必须写入 Redis 键（多 worker 共享窗口）"
    assert int(redis_client.get(keys[0])) == 3


def test_rate_limit_success_login_not_counted(client):
    """成功登录不计数（防误伤）：9 次失败后正确密码可登录，且计数仍为 9。"""
    _wrong_login(client, 9)
    resp = client.post("/api/v1/auth/login",
                       json={"username": "supervisor1", "password": "123456"})
    assert resp.status_code == 200
    assert resp.json()["token"]
    # 成功登录不增加失败计数：窗口内仍剩 1 次失败额度
    keys = redis_client.keys("login:fail:*")
    assert keys and int(redis_client.get(keys[0])) == 9


# ---- 2. human_interventions UNIQUE(case_id)：并发双审批 DB 层兜底 ----
def test_human_intervention_unique_case_id_constraint():
    case_id = f"audit-{uuid.uuid4().hex[:8]}"
    with SessionTest() as s:
        uid = s.query(User).first().id  # seed 用户（外键）
        s.add(RefundCase(
            id=case_id, order_id="ORD-AUDIT", requested_amount=100,
            submitter_id=uid,
            current_status="SUSPENDED", final_decision="PENDING",
            idempotency_key=f"idem-{uuid.uuid4()}",
        ))
        s.commit()
        s.add(HumanIntervention(case_id=case_id, approver_id="x", action="APPROVED",
                                opinion="第一条"))
        s.commit()
        # 第二条同 case_id 必须被 DB 约束拒绝（即使应用层检查被绕过）
        s.add(HumanIntervention(case_id=case_id, approver_id="y", action="REJECTED",
                                opinion="并发穿透"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


# ---- 3. _get_config 30s 缓存：每案件 DB 往返 20 次 -> 1 次 ----
def test_get_config_caches_within_ttl(monkeypatch):
    from app import workflow
    from app.workflow import graph as graph_mod

    graph_mod._config_cache.clear()
    calls = {"n": 0}
    real = graph_mod.SessionLocal

    class _CountingSession:
        def __init__(self):
            calls["n"] += 1
            self._inner = real()

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

        def get(self, *a, **k):
            return self._inner.get(*a, **k)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(graph_mod, "SessionLocal", _CountingSession)
    v1 = graph_mod._get_config("refund.amount_threshold", 300)
    v2 = graph_mod._get_config("refund.amount_threshold", 300)
    v3 = graph_mod._get_config("refund.amount_threshold", 300)
    assert v1 == v2 == v3 == 300
    assert calls["n"] == 1, f"30s 窗口内重复读同一配置应只查一次 DB，实际 {calls['n']} 次"
