# -*- coding: utf-8 -*-
"""能力验证套件端到端（V1.0 修复：eval golden 评审通道验证基线新能力）。

覆盖修复目标：
- 能力 Suite（capabilities_suite）经 worker 真实执行：全部用例 verdict==pass，
  无 SKIPPED、无 degraded（能力已实现）。
- 能力用例不会被误拆进 SKIPPED 通道（concurrency 分支）；golden 预留用例
  （无 capability 字段）仍恒 SKIPPED_UNSUPPORTED。
- 报告 extensions 聚合真实能力证据（security/intent/voice/coaching）。
"""
import json
import uuid

from app.domain import models
from app.services import eval_center as ec
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest

from eval_refund.capabilities_suite import (
    CAPABILITY_CASES,
    build_capability_document,
)
from eval_refund.judge import METRICS


def _cap_doc():
    return build_capability_document()


_PASS = ("pass", "PASS")   # DB 持久化层把 verdict 正名为大写 PASS；runner 内部为小写


def _assert_all_pass(by_id):
    for c in CAPABILITY_CASES:
        row = by_id[c["id"]]
        assert str(row["verdict"]).upper() in ("PASS",), f"{c['id']} {row['verdict']}: {row.get('failure_reason') or row}"


def test_capability_suite_executes_and_passes():
    """10 条能力用例经真实 backend 服务执行 -> 全部 pass（无 SKIPPED/degraded）。"""
    s = SessionTest()
    doc = _cap_doc()
    suite = ec.create_suite_from_golden(
        s, name=f"cap-{uuid.uuid4().hex[:8]}", changelog="capability-suite-v1",
        import_doc=doc)
    assert suite["case_count"] == len(CAPABILITY_CASES)

    run = ec.create_run(s, run_id=f"run-cap-{uuid.uuid4().hex[:10]}",
                        name="capability-suite", suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "cap1", {"case_id": run.run_id, "run_id": run.run_id,
                 "suite_id": suite["id"],
                 "config": json.dumps({"mode": "in_process", "limit": 0})})
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED", got
    results = ec.list_run_results(SessionTest(), run.run_id)
    by_id = {r["case_id"]: r for r in results}
    # 全部能力用例真实执行且通过（无 SKIPPED/degraded）
    _assert_all_pass(by_id)
    assert got["summary"]["failed"] == 0
    assert got["summary"]["degraded"] == 0
    assert got["summary"]["skipped_unsupported"] == 0


def test_capability_suite_parallel_executes_no_skipped():
    """concurrency>1 时能力用例真实并行执行，不被误拆进 SKIPPED 通道。"""
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"cap-par-{uuid.uuid4().hex[:8]}", changelog="cap-suite-par",
        import_doc=_cap_doc())
    run = ec.create_run(s, run_id=f"run-cappar-{uuid.uuid4().hex[:10]}",
                        name="capability-suite-par",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test",
                        config={"mode": "in_process", "concurrency": 4})
    EvalRunConsumer(session_factory=SessionTest).process(
        "cap2", {"case_id": run.run_id, "run_id": run.run_id,
                 "suite_id": suite["id"],
                 "config": json.dumps({"mode": "in_process", "concurrency": 4})})
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED", got
    results = ec.list_run_results(SessionTest(), run.run_id)
    by_id = {r["case_id"]: r for r in results}
    _assert_all_pass(by_id)
    assert got["summary"]["skipped_unsupported"] == 0
    assert got["summary"]["passed"] == len(CAPABILITY_CASES)


def test_golden_reserved_cases_still_skipped_after_plugin():
    """插件安装后，golden 预留用例（无 capability 字段）仍恒 SKIPPED_UNSUPPORTED。"""
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"m21-frozen-{uuid.uuid4().hex[:8]}", changelog="M21 reserved")
    run = ec.create_run(s, run_id=f"run-frozen-{uuid.uuid4().hex[:10]}",
                        name="M21 reserved frozen",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "m21fx", {"case_id": run.run_id, "run_id": run.run_id,
                  "suite_id": suite["id"],
                  "config": json.dumps({"mode": "in_process", "limit": 0})})
    results = ec.list_run_results(SessionTest(), run.run_id)
    by_id = {r["case_id"]: r for r in results}
    for cid in (f"G{i:02d}" for i in range(143, 151)):
        assert by_id[cid]["verdict"] == "SKIPPED_UNSUPPORTED", cid
    # G21 voice 标签预留用例同样保持 SKIPPED
    assert by_id["G21"]["verdict"] == "SKIPPED_UNSUPPORTED"


def test_cap_suite_report_aggregates_extensions():
    """报告 extensions 聚合真实能力证据（非恒 None）。"""
    from app.worker.eval_processor import _archive
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"cap-agg-{uuid.uuid4().hex[:8]}", changelog="cap-suite-agg",
        import_doc=_cap_doc())
    run = ec.create_run(s, run_id=f"run-capagg-{uuid.uuid4().hex[:10]}",
                        name="capability-suite-agg",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "cap3", {"case_id": run.run_id, "run_id": run.run_id,
                 "suite_id": suite["id"],
                 "config": json.dumps({"mode": "in_process", "limit": 0})})
    results = ec.list_run_results(SessionTest(), run.run_id) or []
    # 聚合四个能力的真实证据总条数应 > 0（任一能力已实现即有实测记录）
    rows = [r.get("candidate") or {} for r in results]
    ext_vals = [(r.get("extensions") or {}).get(cap) for r in rows for cap in
                ("security", "intent", "voice", "coaching")]
    assert any(ext_vals), "能力用例的 candidate.extensions 应携带实测证据"


def test_capability_metrics_registered_with_compute():
    """修复后四组 judge.METRICS 均注册真实 compute（非空实现冒充）。"""
    plugin_names = ("security.redline", "intent.confusion", "voice.asr",
                    "coaching.custom")
    for m in plugin_names:
        entry = METRICS.get(m)
        assert entry is not None, f"{m} 未注册"
        assert entry["registered"] is True
        assert entry["compute"] is not None