# -*- coding: utf-8 -*-
"""意图路由 周期意图对照评测。

覆盖：120 条均衡数据集、四类指标、纯 LLM/双层对照、
Evaluation Worker 专项分流、intent 周期调度与 supervisor 一键入口。
"""
from __future__ import annotations

import json
import uuid

from app.domain import models
from app.services import eval_center as ec
from app.services import intent_periodic_eval as ipe
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _api_h(client, username="supervisor1"):
    return _auth_headers(login(client, username))


def test_intent_dataset_is_versioned_balanced_and_at_least_100():
    rows = ipe.build_intent_test_set()
    assert len(rows) == 120
    assert len({row["case_id"] for row in rows}) == 120
    assert len(ipe.dataset_checksum(rows)) == 64
    counts = {intent: sum(row["expected"] == intent for row in rows)
              for intent in ipe.VALID_INTENTS}
    assert set(counts.values()) == {20}


def test_offline_intent_benchmark_reports_required_metrics_and_comparison():
    result = ipe.evaluate_intent_routes(use_live_llm=False, include_shopping_flow=False)
    summary = result["summary"]
    perf = result["performance"]
    cost = result["cost"]

    assert summary["total"] == 120
    assert summary["coverage"] == 1.0
    assert summary["macro_recall"] is not None
    assert summary["hallucination_rate"] is not None
    assert set(summary["hybrid"]["per_intent"]) == set(ipe.VALID_INTENTS)
    assert perf["ttft_ms"]["pure_llm"]["avg"] > 0
    assert perf["ttft_ms"]["hybrid"]["avg"] >= 0
    assert perf["llm_calls"]["hybrid"] < perf["llm_calls"]["pure_llm"]
    assert cost["pure_llm_tokens"] > cost["hybrid_tokens"]
    assert cost["token_gain"] >= ipe.TARGET_TOKEN_GAIN
    assert cost["measured"] is False
    assert result["measurement_mode"] == "offline_replay"


def test_worker_executes_intent_run_without_refund_tier_hydration(monkeypatch):
    from app.services import shopping_flow_eval as sfe

    monkeypatch.setattr(ipe.settings, "INTENT_EVAL_USE_LIVE_LLM", False)
    monkeypatch.setattr(
        sfe, "evaluate_shopping_return_exchange_flows",
        lambda sample_count=100: {
            "status": "COMPLETED", "measured": False, "measurement": "test_double",
            "sample_count": sample_count, "unique_users": sample_count,
            "attempted": sample_count, "completed": sample_count,
            "completion_rate": 1.0, "order_accuracy": 1.0,
            "product_accuracy": 1.0, "amount_accuracy": 1.0,
            "duration_ms": 1.0, "ocr_latency_ms": {"avg": 0.01, "p95": 0.01, "max": 0.01},
            "aftersale_distribution": {"return": 50, "exchange": 50},
            "isolation": {"business_writes": 0, "real_refunds": 0, "mode": "EVAL_IN_MEMORY_ONLY"},
            "dataset_version": "test-shopping", "checksum": "0" * 64,
        },
    )
    s = SessionTest()
    run = ipe.create_intent_eval_run(
        s, created_by="test", trigger_source="manual",
    )
    EvalRunConsumer(session_factory=SessionTest).process(
        "intent-worker-1",
        {
            "case_id": run.run_id,
            "run_id": run.run_id,
            "suite_id": run.suite_id or "",
            "target_type": "intent_router",
            "config": json.dumps(run.config or {}, ensure_ascii=False),
        },
    )
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == models.EVAL_RUN_COMPLETED
    assert got["target_type"] == "intent_router"
    assert got["task_type"] == "intent_eval"
    assert got["summary"]["total"] == 120
    assert got["summary"]["total_cases"] == 220
    assert got["summary"]["shopping_flow"]["sample_count"] == 100
    assert got["performance"]["ttft_ms"]["measurement"] == "estimated_offline"
    assert got["config"]["read_only"] is True


def test_intent_tier_schedule_creates_specialized_run():
    s = SessionTest()
    # 共享测试库只增不删：先终止历史未消费的意图 Run，
    # 以便只验证本调度的创建语义。
    for active in (s.query(models.EvalRun)
                   .filter_by(target_type="intent_router")
                   .filter(models.EvalRun.status.in_([
                       models.EVAL_RUN_PENDING, models.EVAL_RUN_RUNNING,
                   ])).all()):
        active.status = models.EVAL_RUN_CANCELLED
    s.commit()
    suite = ec.create_suite_from_golden(
        s, name=f"intent-schedule-{uuid.uuid4().hex[:8]}", changelog="workorder8",
    )
    schedule = ec.create_schedule(
        s, suite_id=suite["id"], tier="intent", cadence="daily",
        owner_id="test", initial_delay_minutes=0,
    )
    fired = ec.fire_due_schedules(s, limit=10000)
    own = next(item for item in fired["fired"] if item["schedule_id"] == schedule["id"])
    run = ec.get_run(s, own["run_id"])
    assert own["tier"] == "intent"
    assert run["task_type"] == "intent_eval"
    assert run["target_type"] == "intent_router"
    assert run["trigger_source"] == "schedule"
    assert ec.get_schedule(s, schedule["id"])["last_run_id"] == run["run_id"]


def test_supervisor_can_start_and_read_intent_evaluation(client):
    h = _api_h(client)
    created = client.post(
        "/api/v1/evaluation-center/intent-evaluation/runs", headers=h,
    )
    assert created.status_code == 202, created.text
    body = created.json()
    assert body["run_id"]

    listed = client.get(
        "/api/v1/evaluation-center/intent-evaluation/runs", headers=h,
    )
    assert listed.status_code == 200
    assert any(item["run_id"] == body["run_id"] for item in listed.json()["items"])

    latest = client.get(
        "/api/v1/evaluation-center/intent-evaluation/latest", headers=h,
    )
    assert latest.status_code == 200
    assert latest.json()["dataset"]["sample_count"] == 120
