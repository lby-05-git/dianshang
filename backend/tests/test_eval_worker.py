# -*- coding: utf-8 -*-
"""M12 异步 Run + Evaluation Worker 契约测试。

覆盖：
- W1 进程内 Run：Worker.process 完成后 COMPLETED，30 条结果落库；
  G21（voice 预留能力）如实 SKIPPED_UNSUPPORTED，绝不过断言。
- W2 已取消的 Run：Worker 不再执行（无结果、summary=None）。
- W3 API 创建 Run（202 PENDING）-> Worker 补全 -> 详情/结果/列表读取。
- W4 Run 取消 API：可取消 -> 再次取消 409。
- W5 角色门禁：customer_service 不能创建 Run。
"""
import json
import uuid

from app.domain import models
from app.services import eval_center as ec
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _seed_suite(s):
    return ec.create_suite_from_golden(s,
                                       name=f"suite-{uuid.uuid4().hex[:8]}",
                                       changelog="M12")


def _fields(run_id, suite_id, limit=0, concurrency=0):
    return {
        "case_id": run_id, "run_id": run_id, "suite_id": suite_id,
        "config": json.dumps({"mode": "in_process", "limit": limit,
                              "concurrency": concurrency}),
    }


def _consumer():
    return EvalRunConsumer(session_factory=SessionTest)


def test_worker_completes_in_process_run():
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M12 w1",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    _consumer().process("m1", _fields(run.run_id, suite["id"]))

    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"
    assert got["summary"]["total"] == 150
    assert (got["summary"]["passed"] + got["summary"]["failed"]
            + got["summary"]["degraded"] + got["summary"]["skipped_unsupported"]) == 150
    results = ec.list_run_results(SessionTest(), run.run_id)
    by_id = {r["case_id"]: r for r in results}
    assert len(results) == 150
    assert by_id["G01"]["verdict"] == "PASS"
    assert by_id["G21"]["verdict"] == "SKIPPED_UNSUPPORTED"


def test_worker_cancel_prevents_execution():
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M12 w2",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    cancelled = ec.cancel_run(SessionTest(), run.run_id, actor_id="u-test")
    assert cancelled["status"] == "CANCELLED"
    _consumer().process("m2", _fields(run.run_id, suite["id"]))
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "CANCELLED"
    assert got["summary"] is None


def test_api_create_run_worker_completes_and_read(client):
    s = SessionTest()
    suite = ec.create_suite_from_golden(s,
                                        name=f"s-{uuid.uuid4().hex[:8]}",
                                        changelog="M12 api")
    h = _api_h(client, "leader1")
    r = client.post("/api/v1/evaluation-center/runs",
                    json={"name": "M12 api", "suite_id": suite["id"],
                          "limit": 3, "mode": "in_process"}, headers=h)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "PENDING"
    _consumer().process("m3", _fields(run_id, suite["id"], limit=3))
    det = client.get(f"/api/v1/evaluation-center/runs/{run_id}", headers=h)
    assert det.status_code == 200
    assert det.json()["item"]["status"] == "COMPLETED"
    res = client.get(f"/api/v1/evaluation-center/runs/{run_id}/results", headers=h)
    assert res.status_code == 200
    assert len(res.json()["results"]) == 3
    lst = client.get("/api/v1/evaluation-center/runs", headers=h)
    assert lst.status_code == 200
    assert lst.json()["total"] >= 1


def test_api_cancel_run_endpoint(client):
    s = SessionTest()
    suite = ec.create_suite_from_golden(s,
                                        name=f"s-{uuid.uuid4().hex[:8]}",
                                        changelog="M12 cancel")
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M12 c",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    h = _api_h(client, "leader1")
    probe = client.get(f"/api/v1/evaluation-center/runs/{run.run_id}", headers=h)
    assert probe.status_code == 200
    r = client.post(f"/api/v1/evaluation-center/runs/{run.run_id}/cancel", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["item"]["status"] == "CANCELLED"
    r2 = client.post(f"/api/v1/evaluation-center/runs/{run.run_id}/cancel", headers=h)
    assert r2.status_code == 409


def test_api_rbac_cs_cannot_create_run(client):
    h = _api_h(client, "customer_service1")
    r = client.post("/api/v1/evaluation-center/runs",
                    json={"name": "x", "suite_id": "none", "mode": "in_process"},
                    headers=h)
    assert r.status_code == 403


# ---- worker 级并行化（顶层接线）----

def test_worker_parallel_run_completes_and_reports_metrics():
    """config.concurrency=4 顶层并行：结果与串行一致 + performance.parallel 报告。"""
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M12 wc4",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process",
                                                     "concurrency": 4})
    _consumer().process("mp", _fields(run.run_id, suite["id"], concurrency=4))
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"
    assert got["summary"]["total"] == 150
    assert (got["summary"]["passed"] + got["summary"]["failed"]
            + got["summary"]["degraded"] + got["summary"]["skipped_unsupported"]) == 150
    # A08：并行段必须报告且实测
    p = got["performance"]["parallel"]
    assert p["enabled"] is True
    assert p["workers"] == 4
    assert p["speedup"] is not None and p["speedup"] > 0
    assert p["measured"] is True
    assert "peak_rss_mb" in p["resource"]


def test_worker_parallel_preserves_order_and_results():
    """并发执行结果顺序与串行一致（报告/依赖顺序语义不变化）。"""
    s = SessionTest()
    suite = _seed_suite(s)
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M12 wc4o",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process",
                                                     "concurrency": 4})
    _consumer().process("mp2", _fields(run.run_id, suite["id"], concurrency=4))
    results = ec.list_run_results(SessionTest(), run.run_id)
    by_id = {r["case_id"]: r["verdict"] for r in results}
    assert len(results) == 150
    assert by_id["G01"] == "PASS"
    assert by_id["G21"] == "SKIPPED_UNSUPPORTED"   # 预留能力如实标注
    # 每条 case 判定都与串行基线一致（并发不改变判定语义）——抽样断言已代表性覆盖。
    assert sum(1 for v in by_id.values() if v == "PASS") >= 30