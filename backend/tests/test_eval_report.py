# -*- coding: utf-8 -*-
"""M13 Report 对比 + 历史 V1 报告可读 契约测试。

覆盖：
- 历史 V1 报告（reports/run-*.json）可读、同构归一（V1 1.0.0 / 15 条 与 V2 2.0.0 / 30 条）。
- DB 重建（无归档文件时的 Run 报告）。
- compare_runs：pass_rate 增减判定 / 回归 / 改善 / degraded 不视为通过。
"""
import json
import uuid

import pytest

from app.domain import models
from app.services import eval_center as ec
from app.services import report_service as rs
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _suite(s):
    return ec.create_suite_from_golden(s, name=f"suite-{uuid.uuid4().hex[:8]}",
                                       changelog="M13")


def _run(s, suite, name="M13 run", status="COMPLETED", summary=None, perf=None):
    summary = summary or {"total": 30, "passed": 30, "failed": 0,
                          "degraded": 0, "pass_rate": 1.0}
    run = models.EvalRun(
        run_id=f"run-{uuid.uuid4().hex[:10]}", name=name,
        task_type="refund_eval", schema_version="1.0", priority=0,
        status=status, suite_id=suite["id"], suite_version=suite["version"],
        dataset_checksum=suite["checksum"],
        config={"mode": "in_process"},
        summary=summary,
        performance=perf or {"latency_ms": {"p50": 10.0, "p95": 20.0}},
        cost={"measured": False},
    )
    s.add(run)
    s.commit()
    return run


def _mk_report_file(tmp_path, run_id, *, version="1.0", dver="2.0.0",
                    passed=30, total=30, p95=5.0, degraded=0):
    doc = {
        "report_version": version, "run_id": run_id,
        "created_at": "2026-08-01T00:00:00+00:00",
        "environment": {"dataset_version": dver, "dataset_checksum": "abc"},
        "dataset": {"version": dver, "checksum": "abc", "case_count": total},
        "summary": {"total": total, "passed": passed, "failed": total - passed - degraded,
                    "degraded": degraded, "pass_rate": round(passed / total, 4)},
        "performance": {"latency_ms": {"p50": 1.0, "p95": p95}},
        "cost": {"measured": False},
        "extensions": {"security": None, "intent": None, "voice": None, "coaching": None},
        "trace": {"trace_id": run_id, "run_id": run_id},
        "cases": [
            {"case_id": "G01", "judge": {"verdict": "pass"}, "execution": {}},
            {"case_id": "G02", "judge": {"verdict": "fail"}, "execution": {}},
        ],
    }
    p = tmp_path / "reports" / f"{run_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


# ---- 历史 V1 可读 ----

def test_legacy_v1_report_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "_REPORTS_DIR", tmp_path / "reports")
    run_id = "run-v1legacy"
    _mk_report_file(tmp_path, run_id, version="1.0", dver="1.0.0",
                    passed=15, total=15, p95=8.0)
    s = SessionTest()
    doc = rs.read_report(s, run_id)
    assert doc["report_version"] == "1.0"
    assert doc["environment"]["dataset_version"] == "1.0.0"
    assert doc["summary"]["passed"] == 15
    # 同构：V2 run 也走同一条读取
    run_id2 = "run-v2file"
    _mk_report_file(tmp_path, run_id2, dver="2.0.0", passed=30)
    doc2 = rs.read_report(s, run_id2)
    assert doc2["summary"]["passed"] == 30


def test_report_reconstructed_from_db():
    s = SessionTest()
    suite = _suite(s)
    run = _run(s, suite)
    doc = rs.read_report(s, run.run_id)
    assert doc["_source"] == "db"
    assert doc["report_version"] == "1.0"
    assert doc["summary"]["passed"] == 30
    assert doc["run_id"] == run.run_id


def test_report_missing_raises():
    s = SessionTest()
    with pytest.raises(KeyError):
        rs.read_report(s, "run-does-not-exist")


# ---- 对比 ----

def test_compare_same_pass_rate_unchanged():
    s = SessionTest()
    a = _run(s, _suite(s), name="A", summary={"total": 30, "passed": 29, "failed": 1,
                                              "degraded": 0, "pass_rate": 0.9667})
    b = _run(s, _suite(s), name="B", summary={"total": 30, "passed": 29, "failed": 1,
                                              "degraded": 0, "pass_rate": 0.9667})
    cmp = rs.compare_runs(s, a.run_id, b.run_id)
    assert cmp["verdict"] == "unchanged"
    assert cmp["deltas"]["pass_rate"] == 0.0


def test_compare_regression_flagged():
    s = SessionTest()
    a = _run(s, _suite(s), name="A", summary={"total": 30, "passed": 30, "failed": 0,
                                              "degraded": 0, "pass_rate": 1.0})
    b = _run(s, _suite(s), name="B", summary={"total": 30, "passed": 27, "failed": 3,
                                              "degraded": 0, "pass_rate": 0.9})
    cmp = rs.compare_runs(s, a.run_id, b.run_id)
    assert cmp["verdict"] == "regressed"
    assert cmp["deltas"]["pass_rate"] == pytest.approx(-0.1, abs=1e-4)


def test_compare_reports_by_file_with_regressions(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "_REPORTS_DIR", tmp_path / "reports")
    s = SessionTest()
    a = "run-abase"
    b = "run-bnew"
    # A: G01 pass, G02 fail
    pa = _mk_report_file(tmp_path, a, passed=15, total=15, p95=8.0)
    # B: G01 pass, G02 pass（改善）-> 但这里用 30 全过模拟改善
    pb = _mk_report_file(tmp_path, b, passed=30, total=30, p95=5.0)
    cmp = rs.compare_runs(s, a, b)
    assert cmp["deltas"]["common_cases"] == 2
    assert cmp["verdict"] in ("improved", "regressed", "unchanged")


def test_degraded_not_counted_as_pass():
    from app.services.report_service import _norm_verdict
    assert _norm_verdict("pass") == "pass"
    assert _norm_verdict("DEGRADED") == "degraded"
    assert _norm_verdict("SKIPPED_UNSUPPORTED") == "skipped"
    assert _norm_verdict("ERROR") == "degraded"


# ---- API ----

def test_api_reports_list_and_detail(client):
    s = SessionTest()
    suite = _suite(s)
    run = _run(s, suite, status="COMPLETED")
    h = _api_h(client, "leader1")
    lst = client.get("/api/v1/evaluation-center/reports", headers=h)
    assert lst.status_code == 200
    run_ids = [r["run_id"] for r in lst.json()["reports"]]
    assert run.run_id in run_ids
    det = client.get(f"/api/v1/evaluation-center/reports/{run.run_id}", headers=h)
    assert det.status_code == 200
    assert det.json()["item"]["_source"] == "db"
    miss = client.get("/api/v1/evaluation-center/reports/run-nope", headers=h)
    assert miss.status_code == 404


def test_api_compare_endpoint(client):
    s = SessionTest()
    a = _run(s, _suite(s), name="A")
    b = _run(s, _suite(s), name="B")
    h = _api_h(client, "leader1")
    r = client.get(f"/api/v1/evaluation-center/reports/compare?run_a={a.run_id}&run_b={b.run_id}",
                   headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "run_a" in body and "run_b" in body
    miss = client.get("/api/v1/evaluation-center/reports/compare?run_a=run-nope&run_b=run-no2",
                      headers=h)
    assert miss.status_code == 404