# -*- coding: utf-8 -*-
"""M7 Runner + JSON 报告 契约测试（F15-A/C/O 与报告结构）。"""
import json
import time

import pytest

from eval_refund import runner
from eval_refund.golden import GOLDEN_CASES, build_dataset_document
from eval_refund.schema import (
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    STATUS_COMPLETED,
    STATUS_SUSPENDED,
    CandidateOutput,
)


class _Executor:
    """进程内假执行器：按 case_id 返回匹配 Golden 的输出（确定性，无网络）。"""

    def __init__(self, output_overrides: dict | None = None) -> None:
        self.overrides = output_overrides or {}
        self.calls: list[str] = []
        self.delay_ms = 0.0

    def __call__(self, case: dict, ctx: dict) -> CandidateOutput:
        self.calls.append(case["id"])
        ov = self.overrides.get(case["id"])
        if ov is not None:
            return ov
        node = "finalize"
        if case["expected"]["current_status"] == STATUS_SUSPENDED:
            node = "human_review"
        out = CandidateOutput(
            case_id=case["id"],
            current_status=case["expected"]["current_status"],
            final_decision=case["expected"]["final_decision"],
            refund_status=case["expected"]["refund_status"],
            current_graph_node=node,
            order_verification=case["expected"].get("order_verification", ""),
            product_consistency=case["expected"].get("product_consistency", ""),
            price_consistency=case["expected"].get("price_consistency", ""),
            latency_ms=self.delay_ms,
        )
        return out


def _executor_all_pass() -> _Executor:
    return _Executor()


def test_run_all_cases_pass():
    ex = _Executor()
    report = runner.run_eval(executor=ex)
    assert report["summary"]["total"] == 150
    assert report["summary"]["passed"] == 150
    assert report["summary"]["failed"] == 0
    assert report["summary"]["degraded"] == 0
    assert report["summary"]["pass_rate"] == 1.0
    assert len(ex.calls) == 150


def test_run_limit():
    ex = _Executor()
    report = runner.run_eval(executor=ex, limit=3)
    assert report["summary"]["total"] == 3
    assert len(ex.calls) == 3
    assert report["dataset"]["limit"] == 3


def test_report_structure():
    report = runner.run_eval(executor=_Executor(), limit=5)
    # 报告头关系字段
    assert report["report_version"] == "1.0"
    assert report["run_id"].startswith("run-")
    assert "created_at" in report
    assert report["environment"]["dataset_version"] == "3.0.0"
    assert report["environment"]["dataset_checksum"] == build_dataset_document()["checksum"]
    # 数据集头
    assert report["dataset"]["checksum"] == build_dataset_document()["checksum"]
    # 性能
    assert "p50" in report["performance"]["latency_ms"]
    assert "p95" in report["performance"]["latency_ms"]
    # 成本（未实测 -> measured=False，绝不伪造）
    assert report["cost"]["measured"] is False
    # extensions 占位
    assert set(report["extensions"]) == {"security", "intent", "voice", "coaching"}
    # case 级
    assert len(report["cases"]) == 5
    c = report["cases"][0]
    assert set(c) >= {"case_id", "judge", "execution", "candidate", "degraded"}
    assert c["judge"]["judge_version"] == "1.0"


def test_failed_case_counts():
    ex = _Executor()
    ex.overrides["G03"] = CandidateOutput(
        case_id="G03", current_status=STATUS_COMPLETED,
        # G03 期望 REJECTED 但返回 APPROVED
        final_decision=DECISION_APPROVED, refund_status="SUCCESS",
        current_graph_node="finalize")
    report = runner.run_eval(executor=ex)
    assert report["summary"]["failed"] == 1
    assert report["cases"][2]["judge"]["verdict"] == "fail"  # G03 是第 3 条


def test_degraded_when_executor_throws():
    def boom(case, ctx):
        raise TimeoutError("target down")
    report = runner.run_eval(executor=boom, limit=2)
    assert report["summary"]["degraded"] == 2
    assert report["summary"]["passed"] == 0
    for c in report["cases"]:
        assert c["degraded"] is True
        assert c["judge"]["verdict"] == "degraded"
        assert "TimeoutError" in c["error"]


def test_checksum_mismatch_raises():
    with pytest.raises(ValueError, match="校验和"):
        runner.run_eval(executor=_Executor(),
                        dataset_checksum="deadbeef")


def test_p50_p95_computed():
    lat = [10.0, 20.0, 30.0, 40.0, 50.0]
    stat = runner._p50p95(lat)
    assert stat["count"] == 5
    assert stat["p50"] == 30.0
    assert stat["p95"] > 30.0
    # 空列表安全
    assert runner._p50p95([])["count"] == 0


def test_report_written_to_file(tmp_path):
    p = tmp_path / "run.json"
    report = runner.run_eval(executor=_Executor(), limit=2, report_path=p)
    assert p.exists()
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["summary"]["total"] == 2
    assert doc["_report_file"] == str(p)


def test_trace_and_run_id_consistent():
    report = runner.run_eval(executor=_Executor(), limit=1)
    assert report["trace"]["run_id"] == report["run_id"]
    assert report["trace"]["trace_id"] == report["run_id"]


def test_llm_judge_augments_report_when_available(tmp_path):
    def fake_llm(out, case):
        return {"judged": True, "fallback": False,
                "dimensions": {"a": 4}, "summary": "ok"}
    report = runner.run_eval(executor=_Executor(), limit=1,
                             use_llm=True, llm_cb=fake_llm)
    c = report["cases"][0]
    assert c["judge"]["llm"]["judged"] is True


def test_llm_judge_absent_keeps_null():
    report = runner.run_eval(executor=_Executor(), limit=1, use_llm=False)
    assert report["cases"][0]["judge"]["llm"] is None


@pytest.mark.smoke
def test_eval_smoke_offline_runs_json_report(tmp_path):
    """make eval-smoke：离线确定性跑 3 条并生成 JSON 报告（无需起 server）。"""
    p = tmp_path / "smoke.json"
    report = runner.run_eval(executor=_Executor(), limit=3, report_path=p)
    assert report["summary"]["passed"] == 3
    assert p.exists()


@pytest.mark.smoke
def test_eval_smoke_schema_contract():
    from eval_refund.schema import SCHEMA_VERSION
    assert SCHEMA_VERSION == "1.0"


# ---- 并行失败隔离（asyncio.gather(return_exceptions=True) 等价）----

def test_parallel_keeps_original_order():
    """并发执行必须保持 golden 原始顺序（报告/幂等依赖顺序语义）。"""
    ex = _Executor()
    report = runner.run_eval(executor=ex, parallel_workers=4, limit=10)
    ids = [c["case_id"] for c in report["cases"]]
    assert ids == [g["id"] for g in GOLDEN_CASES[:10]]
    assert report["performance"]["parallel"]["enabled"] is True
    assert report["performance"]["parallel"]["workers"] == 4


def test_parallel_failure_isolation():
    """个别用例抛异常 -> 只该条 degraded，其余全部照常执行（A03）。"""
    calls: list[str] = []

    def flaky(case, ctx):
        calls.append(case["id"])
        if case["id"] in ("G02", "G05"):
            raise TimeoutError(f"target down for {case['id']}")
        return _Executor()(case, ctx)

    report = runner.run_eval(executor=flaky, parallel_workers=4, limit=8)
    assert report["summary"]["degraded"] == 2
    assert report["summary"]["passed"] == 6
    by_id = {c["case_id"]: c for c in report["cases"]}
    assert by_id["G02"]["error"] and "TimeoutError" in by_id["G02"]["error"]
    assert by_id["G05"]["error"] and "TimeoutError" in by_id["G05"]["error"]
    assert by_id["G01"]["judge"]["verdict"] == "pass"        # 未受影响
    assert by_id["G08"]["judge"]["verdict"] == "pass"        # 未受影响
    # A03：失败不取消其他任务 —— 8 条全部进入判定（degraded 而非消失）。
    # 并发模式额外补跑一次串行基线测速（A08），故调用次数 = 8(并行)+8(串行基线)。
    assert len(calls) == 16


def test_serial_parallel_identical_passes():
    """串行与并行的判定结论必须完全一致（并行不改变评测语义）。"""
    ex1 = _Executor()
    ex2 = _Executor()
    r_ser = runner.run_eval(executor=ex1, parallel_workers=0, limit=6)
    r_par = runner.run_eval(executor=ex2, parallel_workers=3, limit=6)
    assert r_ser["summary"] == r_par["summary"]
    assert [c["judge"]["verdict"] for c in r_ser["cases"]] == \
           [c["judge"]["verdict"] for c in r_par["cases"]]


# ---- 并行化时延与资源度量（速度/错误/资源三要素）----

def test_parallel_metrics_reported():
    """A08 报告 performance.parallel 段：速度/错误/资源三要素齐备。"""
    ex = _Executor()
    report = runner.run_eval(executor=ex, parallel_workers=4, limit=10)
    p = report["performance"]["parallel"]
    assert p["enabled"] is True
    assert p["workers"] == 4
    assert isinstance(p["wall_ms"], (int, float)) and p["wall_ms"] > 0
    assert p["serial_wall_ms"] is not None
    assert p["speedup"] is not None and p["speedup"] > 0
    assert p["error_count"] == 0
    assert "peak_rss_mb" in p["resource"]


def test_parallel_speedup_reported_real():
    """并发确比串行快（executor 带 sleep 时）：speedup>1 且实测非伪造。"""
    class _SlowExec(_Executor):
        def __call__(self, case, ctx):
            time.sleep(0.02)      # 模拟每条约 20ms 处理
            return super().__call__(case, ctx)

    report = runner.run_eval(executor=_SlowExec(), parallel_workers=4, limit=12)
    p = report["performance"]["parallel"]
    assert p["enabled"] is True
    assert p["serial_wall_ms"] > 0
    assert p["wall_ms"] > 0
    # 4 线程 12 条 * 20ms：理想 80ms vs 240ms 串行；容差下断言加速
    assert p["speedup"] > 1.0, f"期望加速比>1，实际 {p['speedup']}"
    assert p["measured"] is True


def test_serial_run_parallel_disabled():
    """串行模式（默认）performance.parallel.enabled 必须为 False。"""
    report = runner.run_eval(executor=_Executor(), limit=2)
    p = report["performance"]["parallel"]
    assert p["enabled"] is False
    assert p["workers"] == 0
    assert p["speedup"] is None