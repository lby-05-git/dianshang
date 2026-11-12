# -*- coding: utf-8 -*-
"""M18 分级调度契约测试（持续评测治理）。

覆盖：
- S1 service：create_schedule 幂等、tier 校验、suite 校验。
- S2 service：due_schedules 只在到期/启用时返回；mark_schedule_fired 重排。
- S3 service：suite_tier_cases 从 tier_map 过滤（smoke⊂core⊂full 语义）。
- S4 worker：config.tier 过滤触发 tier 级 Run，结果只含该 tier 用例。
- S5 API：创建/列表/详情/启停/手动触发（调度只触发 Run，绝不自动改决策）。
- S6 红线：停用调度触发被拒；非 APPROVED Prompt 不因调度绕开（创建 Run 校验）。
"""
import json
import uuid

import pytest

from app.domain import models
from app.services import eval_center as ec
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s, name=None) -> dict:
    return ec.create_suite_from_golden(
        s, name=name or f"suite-{uuid.uuid4().hex[:8]}", changelog="M18")


def _mk_schedule(s, suite, tier="smoke", cadence="manual", **kw):
    return ec.create_schedule(s, suite_id=suite["id"], tier=tier,
                              cadence=cadence, owner_id="u-test", **kw)


# ---- S1 / S2 service ----

def test_create_schedule_idempotent_and_validated():
    s = SessionTest()
    suite = _seed_suite(s)
    a = _mk_schedule(s, suite, tier="smoke", cadence="daily")
    b = ec.create_schedule(s, suite_id=suite["id"], tier="smoke",
                           cadence="weekly")   # 同 suite+tier -> 返回已有
    assert a["id"] == b["id"]
    assert a["tier"] == "smoke"
    assert a["cadence"] == "daily"
    # 非法 tier
    with pytest.raises(ValueError, match="tier"):
        ec.create_schedule(s, suite_id=suite["id"], tier="bogus", cadence="daily")
    # 不存在的 suite
    with pytest.raises(KeyError):
        ec.create_schedule(s, suite_id="suite-nope", tier="smoke", cadence="daily")


def test_due_schedules_only_enabled_and_past_due():
    s = SessionTest()
    suite = _seed_suite(s)
    _mk_schedule(s, suite, tier="smoke", cadence="daily", initial_delay_minutes=0)
    # 测试库全局共享且只增不减：due_schedules 默认 limit=20（按 next_due 升序截断），
    # 历史 run 堆积的到期调度可能占满窗口，使新插调度落在窗口外 —— 这里显式放大
    # limit，只测「启用 + 已到期」语义，不依赖全局行数运气。
    due = ec.due_schedules(s, limit=1000)
    assert any(x["tier"] == "smoke" and x["suite_id"] == suite["id"] for x in due)
    # 未来到期（initial_delay 60min）不算 due
    _mk_schedule(s, suite, tier="core", cadence="daily",
                 initial_delay_minutes=60)
    due_ids = {x["tier"] for x in ec.due_schedules(s, limit=1000)
               if x["suite_id"] == suite["id"]}
    assert "core" not in due_ids
    # 停用后不再 due
    sch_id = _mk_schedule(s, suite, tier="full", cadence="weekly")["id"]
    ec.set_schedule_enabled(s, sch_id, enabled=False)
    due_ids2 = {x["tier"] for x in ec.due_schedules(s, limit=1000)
                if x["suite_id"] == suite["id"]}
    assert "full" not in due_ids2


def test_suite_tier_cases_from_tier_map():
    s = SessionTest()
    suite = _seed_suite(s)
    smoke = ec.suite_tier_cases(s, suite["id"], "smoke")
    core = ec.suite_tier_cases(s, suite["id"], "core")
    full = ec.suite_tier_cases(s, suite["id"], "full")
    assert smoke and core and full
    assert set(smoke) <= set(core) <= set(full)
    assert len(full) == 150
    assert ec.suite_tier_cases(s, suite["id"], "bogus") is None
    assert ec.suite_tier_cases(s, "suite-nope", "smoke") is None


def test_mark_schedule_fired_referrer():
    s = SessionTest()
    suite = _seed_suite(s)
    sch = _mk_schedule(s, suite, tier="smoke", cadence="daily")
    from datetime import datetime, timezone
    after = datetime.now(timezone.utc).replace(tzinfo=None)
    # 触发后 last_run_id 落链 + next_due 重排（daily -> +1d）
    from app.services.eval_center import _next_due
    ec.mark_schedule_fired(s, sch["id"], run_id="run-x",
                           next_due=_next_due("daily", after))
    got = ec.get_schedule(s, sch["id"])
    assert got["last_run_id"] == "run-x"
    assert got["next_due_at"] and got["next_due_at"] > after.isoformat()


# ---- S4 worker：tier 过滤 ----

def test_worker_runs_tier_subset():
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(
        s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M18 smoke",
        suite=s.get(models.EvalSuite, suite["id"]),
        created_by="u-test", config={"mode": "in_process", "tier": "smoke"})
    fields = {
        "case_id": run.run_id, "run_id": run.run_id, "suite_id": suite["id"],
        "config": json.dumps({"mode": "in_process", "tier": "smoke"}),
    }
    EvalRunConsumer(session_factory=SessionTest).process("m18s1", fields)
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"
    assert got["summary"]["total"] == 6
    assert got["summary"]["passed"] == 6
    results = ec.list_run_results(SessionTest(), run.run_id)
    assert {r["case_id"] for r in results} == set(ec.suite_tier_cases(
        SessionTest(), suite["id"], "smoke"))


def test_worker_unknown_tier_fails_not_silent_full():
    """数据集未声明 tier -> 明确 FAIL，绝不静默全量。"""
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(
        s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M18 bogus tier",
        suite=s.get(models.EvalSuite, suite["id"]),
        created_by="u-test",
        config={"mode": "in_process", "tier": "bogus"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "m18b1", {"case_id": run.run_id, "run_id": run.run_id,
                  "suite_id": suite["id"],
                  "config": json.dumps({"mode": "in_process", "tier": "bogus"})})
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] in ("FAILED", "CANCELLED")
    assert got["summary"] is None


# ---- S5 / S6 API ----

def test_api_schedule_crud_and_fire(client):
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"s-{uuid.uuid4().hex[:6]}", changelog="M18 api", owner_id="u")
    h = _api_h(client, "leader1")

    # 创建：smoke/daily
    r = client.post("/api/v1/evaluation-center/schedules",
                    json={"suite_id": suite["id"], "tier": "smoke",
                          "cadence": "daily", "enabled": True}, headers=h)
    assert r.status_code == 201, r.text
    sch = r.json()["item"]
    assert sch["tier"] == "smoke"
    assert sch["suite_name"] == suite["name"]

    # 列表
    lst = client.get("/api/v1/evaluation-center/schedules", headers=h)
    assert lst.status_code == 200
    assert lst.json()["total"] >= 1

    # 详情
    det = client.get(f"/api/v1/evaluation-center/schedules/{sch['id']}", headers=h)
    assert det.status_code == 200
    assert det.json()["item"]["id"] == sch["id"]

    # 停用 -> 停用触发被拒（409）
    upd = client.patch(f"/api/v1/evaluation-center/schedules/{sch['id']}",
                       json={"enabled": False}, headers=h)
    assert upd.status_code == 200
    assert upd.json()["item"]["enabled"] is False
    fire = client.post(f"/api/v1/evaluation-center/schedules/{sch['id']}/fire",
                       headers=h)
    assert fire.status_code == 409

    # 重新启用 -> 可触发：创建 tier Run 并入队（202）
    client.patch(f"/api/v1/evaluation-center/schedules/{sch['id']}",
                 json={"enabled": True}, headers=h)
    fired = client.post(f"/api/v1/evaluation-center/schedules/{sch['id']}/fire",
                        headers=h)
    assert fired.status_code == 202, fired.text
    body = fired.json()
    assert body["tier"] == "smoke"
    assert body["run_id"].startswith("run-")
    # 调度证据链：last_run_id 已记录
    det2 = client.get(f"/api/v1/evaluation-center/schedules/{sch['id']}", headers=h)
    assert det2.json()["item"]["last_run_id"] == body["run_id"]


def test_api_rbac_cs_cannot_manage_schedules(client):
    h = _api_h(client, "customer_service1")
    r = client.get("/api/v1/evaluation-center/schedules", headers=h)
    assert r.status_code == 403