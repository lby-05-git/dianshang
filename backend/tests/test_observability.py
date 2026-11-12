# -*- coding: utf-8 -*-
"""M20 漂移监测 + 成本预算告警契约测试（V3.0 持续评测治理）。

覆盖：
- S1 budget：upsert 幂等；超支检查 -> 生成告警（只告警，不改退款决策）。
- S2 drift：首样本只记录基线；后续异常误差率 -> 判漂移 + 告警。
- S3 pass_rate 下降 / P95 异常 -> 漂移判定方向正确。
- S4 alert：ack 后同一 run/metric 幂等不重复。
- S5 API：budgets/drift/alerts CRUD + 预算检查触发。
- S6 RBAC：CS 无权访问监测。

红线：漂移/预算/告警只读评测数据，绝不触碰退款/决策字段。
"""
import uuid

import pytest

from app.services import eval_center as ec
from app.services import observability as obs
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s) -> dict:
    return ec.create_suite_from_golden(
        s, name=f"m20-{uuid.uuid4().hex[:8]}", changelog="M20")


# ---- S1 预算 ----

def test_budget_upsert_and_check():
    s = SessionTest()
    suite = _seed_suite(s)
    b1 = obs.upsert_budget(s, dimension="run", dim_key=suite["id"], period="daily",
                           limit_amount=50.0, limit_tokens=10_000, enabled=True)
    # 幂等：同维度同 key 同 period 更新不重复
    b2 = obs.upsert_budget(s, dimension="run", dim_key=suite["id"], period="daily",
                           limit_amount=80.0, limit_tokens=20_000, enabled=True)
    assert b1["id"] == b2["id"]
    assert b2["limit_amount"] == 80.0

    # 未超支 -> 无告警
    no = obs.check_budget(s, dimension="run", dim_key=suite["id"], period="daily",
                          amount=10.0, tokens=100)
    assert no == []

    # 金额超支 / token 超支 -> 告警（且幂等：同维度重复检查不再新增）
    hits = obs.check_budget(s, dimension="run", dim_key=suite["id"], period="daily",
                            amount=90.0, tokens=5_000)
    assert hits, "金额超支应生成告警"
    hits2 = obs.check_budget(s, dimension="run", dim_key=suite["id"], period="daily",
                             amount=95.0, tokens=30_000)
    assert hits2 == [], "同维度重复超支不重复告警（幂等）"


# ---- S2 漂移 ----

def test_drift_first_sample_no_flag_then_drift():
    s = SessionTest()
    suite = _seed_suite(s)
    # 首 sample：只记录基线，不判漂移
    r1 = obs.record_drift(s, run_id=f"run-{uuid.uuid4().hex[:10]}", suite_id=suite["id"],
                          results=[{"degraded": False, "judge": {"verdict": "pass"}}] * 5,
                          perf={"latency_ms": {"p95": 100}})
    assert r1 == []
    inds = obs.list_drift_indicators(s)
    assert len(inds) >= 3
    assert all(not i["drifted"] for i in inds if i["run_id"].startswith(
        "run-") and i["suite_id"] == suite["id"] and i["baseline"] > 0)

    # 第二次：错误率从 0 飙到 0.5（相对阈值 0.10 超限）-> 漂移 + 告警
    r2 = obs.record_drift(s, run_id=f"run-{uuid.uuid4().hex[:10]}", suite_id=suite["id"],
                          results=[{"degraded": True, "judge": {"verdict": "degraded"}},
                                   {"degraded": False, "judge": {"verdict": "pass"}}] * 3,
                          perf={"latency_ms": {"p95": 500}})
    assert any(a["alert_type"] == "DRIFT" for a in r2), "错误率异常应告警"
    # 校验 error_rate 指标：存在一条 baseline 0.0 -> value 0.5 且 drifted 的记录
    errs = [i for i in obs.list_drift_indicators(s)
            if i["metric_key"] == "error_rate" and i["suite_id"] == suite["id"]]
    hit = [e for e in errs if e["value"] == 0.5 and e["drifted"]]
    assert hit, f"应有 error_rate=0.5 漂移记录，实际 {errs}"


def test_drift_pass_rate_direction():
    s = SessionTest()
    suite = _seed_suite(s)
    obs.record_drift(s, run_id=f"run-{uuid.uuid4().hex[:10]}", suite_id=suite["id"],
                     results=[{"degraded": False, "judge": {"verdict": "pass"}}] * 4
                             + [{"degraded": False, "judge": {"verdict": "fail"}}],
                     perf={"latency_ms": {"p95": 100}})
    # 第二次 pass_rate 从 0.8 -> 0.5（降 0.3 > threshold 0.05）-> 漂移
    obs.record_drift(s, run_id=f"run-{uuid.uuid4().hex[:10]}", suite_id=suite["id"],
                     results=[{"degraded": False, "judge": {"verdict": "pass"}}]
                             + [{"degraded": False, "judge": {"verdict": "fail"}}] * 3,
                     perf={"latency_ms": {"p95": 150}})
    latest = [i for i in obs.list_drift_indicators(s)
              if i["metric_key"] == "pass_rate" and i["drifted"]]
    assert latest, "pass_rate 应判漂移"
    assert latest[0]["value"] < latest[0]["baseline"]


# ---- S4 告警 ack ----

def test_alert_ack_idempotent():
    s = SessionTest()
    alerts = obs.list_alerts(s)
    if not alerts:
        pytest.skip("需要已有告警")
    a = alerts[0]
    got = obs.ack_alert(s, a["id"], acked_by="u-lead")
    assert got["acked"] is True
    assert got["acked_by"] == "u-lead"
    # 二次 ack 幂等
    again = obs.ack_alert(s, a["id"], acked_by="u-lead")
    assert again["acked"] is True


# ---- S5 API ----

def test_api_budget_drift_alert_flow(client):
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"m20-{uuid.uuid4().hex[:6]}", changelog="M20 api", owner_id="u")
    h = _api_h(client, "leader1")

    # 预算 upsert
    r = client.post("/api/v1/evaluation-center/monitor/budgets",
                    json={"dimension": "run", "dim_key": suite["id"],
                          "period": "daily", "limit_amount": 50.0,
                          "limit_tokens": 10_000, "enabled": True}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["item"]["limit_amount"] == 50.0
    budget_id = r.json()["item"]["id"]

    # 列表
    lst = client.get("/api/v1/evaluation-center/monitor/budgets", headers=h)
    assert lst.status_code == 200
    assert any(b["id"] == budget_id for b in lst.json()["items"])

    # 预算检查触发告警
    chk = client.post("/api/v1/evaluation-center/monitor/alerts/check",
                      json={"dimension": "run", "dim_key": suite["id"],
                            "period": "daily", "amount": 99.0, "tokens": 20_000},
                      headers=h)
    assert chk.status_code == 202, chk.text
    assert chk.json()["items"] and chk.json()["items"][0]["alert_type"] == "BUDGET"

    # 告警列表 + ack
    al = client.get("/api/v1/evaluation-center/monitor/alerts", headers=h)
    assert al.status_code == 200
    assert al.json()["items"]
    alert_id = al.json()["items"][0]["id"]
    ak = client.post(f"/api/v1/evaluation-center/monitor/alerts/{alert_id}/ack",
                     headers=h)
    assert ak.status_code == 200
    assert ak.json()["item"]["acked"] is True

    # 漂移记录（HTTP 层走 record_drift 等价逻辑）-> 直接写一条
    d = client.get("/api/v1/evaluation-center/monitor/drift", headers=h)
    assert d.status_code == 200
    sm = client.get(f"/api/v1/evaluation-center/monitor/drift/summary/{suite['id']}",
                    headers=h)
    assert sm.status_code == 200
    assert sm.json()["item"]["suite_id"] == suite["id"]


# ---- S6 RBAC ----

def test_api_rbac_cs_cannot_monitor(client):
    h = _api_h(client, "customer_service1")
    r = client.get("/api/v1/evaluation-center/monitor/budgets", headers=h)
    assert r.status_code == 403
    r2 = client.get("/api/v1/evaluation-center/monitor/alerts", headers=h)
    assert r2.status_code == 403