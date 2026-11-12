# -*- coding: utf-8 -*-
"""评测 Runner + JSON 报告（..）。

编排：
  读 Golden Dataset（校验和）-> 依 case 目标执行（HTTP 黑盒 / in-process）->
  确定性 Judge == 可选 LLM 附加 -> 汇总统计（pass/fail/degraded、P50/P95 时延、
  Token/成本，全部实测，未测量标 measured=False）-> 输出 JSON 报告。

报告头：样本量、环境、模型、数据集版本与时间范围。
资金副作用：Judge 按 Golden 一次性判断，同一 case 只执行一次（幂等余额在 G12 由
Judge evidence_cb 汇总）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .golden import DATASET_META, GOLDEN_CASES, build_dataset_document
from .judge import JudgeResult, deterministic_judge
from .schema import CandidateOutput
from .target import RefundSystemHttpTarget, TargetError

# A3：并行失败隔离 —— 单个工具/用例失败不得取消其他独立任务（等价
# asyncio.gather(..., return_exceptions=True)）。这里用线程池的 map（保序）。
# 线程数上限 8：评测数据集 150 条、用例粒度毫秒~秒级，再高吞吐收益递减且
# 资源占用会反向恶化（A8 报告的资源占用）。从业务域取，比裸 asyncio.gather
# 更贴合"HTTP 黑盒 + 进程内执行器"两类用例的实际执行环境。
PARALLEL_MAX_WORKERS = 8

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"

# in-process 执行器占位：HTTP 模式必填 target；离线模式直接注入执行器返回 CandidateOutput
Executor = Callable[[dict, dict], CandidateOutput]   # (str golden, context) -> output


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_report() -> dict:
    import platform
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "base_url": os.getenv("REFUND_SYSTEM_BASE_URL", ""),
        "use_llm_judge": bool(os.getenv("EVAL_LLM_API_KEY") or os.getenv("EVAL_LLM_BASE_URL")),
        "dataset_version": DATASET_META["version"],
        "dataset_checksum": DATASET_META["checksum"],
        # M21 插件契约：被测 Target 能力声明（只反映声明，绝不自证可用）
        "capabilities": _target_capability_declaration(),
    }


def _target_capability_declaration() -> dict:
    """Target 能力声明：默认全 unavailable（无真实插件时不伪报可用）。

    插件注册 real capability 后，这里自然反映 available。永远把
    unimplemented 的预留能力标 unavailable，报告不得据此判 pass。
    """
    try:
        from .capabilities import target_declaration
        return target_declaration()
    except Exception:  # noqa: BLE001 —— 契约模块缺失时也如实报不可用
        return {"security": {"availability": "unavailable"},
                "intent": {"availability": "unavailable"},
                "voice": {"availability": "unavailable", "audio": True},
                "coaching": {"availability": "unavailable"}}


# ---- 时延统计 ----

def _p50p95(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None, "min": None, "max": None, "count": 0}
    vs = sorted(values)
    n = len(vs)

    def idx(p: float) -> float:
        k = (n - 1) * p
        lo = int(k)
        frac = k - lo
        if lo + 1 < n:
            return vs[lo] + frac * (vs[lo + 1] - vs[lo])
        return vs[lo]

    return {
        "p50": round(idx(0.50), 1),
        "p95": round(idx(0.95), 1),
        "min": round(vs[0], 1),
        "max": round(vs[-1], 1),
        "count": n,
    }


# ---- 单条执行 ----

def run_one_case(
    case: dict,
    *,
    target: RefundSystemHttpTarget | None = None,
    executor: Executor | None = None,
    run_id: str = "",
    use_llm: bool = False,
    llm_cb: Callable[[CandidateOutput, dict], dict] | None = None,
) -> dict:
    """执行单条 Golden 并判定，返回 per-case 报告 dict。

    target 与 executor 二选一（HTTP 黑盒 / 进程内注入）。执行失败/超时按 degraded
    记录（不伪造 pass），保留失败原因。
    """
    case_id = case["id"]
    start = time.perf_counter()
    error: str | None = None
    degraded_reason: str | None = None
    out: CandidateOutput | None = None
    try:
        if target is not None:
            out = target.run_case(_inject_run_case(case, run_id))
        elif executor is not None:
            out = executor(case, {"run_id": run_id})
        else:
            raise ValueError("runner 需要 target 或 executor 之一")
    except TargetError as exc:
        error = f"TargetError: {exc}"
        degraded_reason = str(exc)
    except Exception as exc:  # noqa: BLE001 —— 执行异常 -> degraded，不假装 pass
        error = f"executor_error: {exc.__class__.__name__}: {exc}"
        degraded_reason = str(exc)
    wall_ms = round((time.perf_counter() - start) * 1000, 1)

    # 判定
    judge_res: JudgeResult | None = None
    llm_result: dict | None = None
    if out is not None:
        judge_res = deterministic_judge(out, case)
        llm_result = (
            (llm_cb(out, case) if llm_cb else None) if use_llm else None
        )

    judge_dict = judge_res.as_dict() if judge_res else {
        "verdict": "degraded",
        "failure_reason": error or "未产生候选输出",
        "degraded_reason": degraded_reason or "未产生候选输出",
    }
    if llm_result is not None:
        judge_dict["llm"] = llm_result
    else:
        judge_dict["llm"] = None

    return {
        "case_id": case_id,
        "case_type": case.get("case_type", ""),
        "schema_version": "1.0",
        "execution": {
            "mode": "http" if target is not None else "in_process",
            "latency_ms": wall_ms,
            "polling_rounds": getattr(out, "polling_rounds", 0) if out else None,
        },
        "judge": judge_dict,
        "candidate": out.as_dict() if out else None,
        "degraded": out is None,
        "error": error,
    }


def _inject_run_case(case: dict, run_id: str) -> dict:
    c = dict(case)
    c["eval_run_id"] = run_id
    return c


# ---- 并行执行与失败隔离----

def _run_cases(
    cases: list[dict],
    *,
    target: RefundSystemHttpTarget | None,
    executor: Executor | None,
    run_id: str,
    use_llm: bool,
    llm_cb: Callable[[CandidateOutput, dict], dict] | None,
    parallel_workers: int = 0,
) -> tuple[dict, list[dict]]:
    """执行全部用例；返回 (parallel_metrics, 保持原顺序的 results)。

    A03 失败隔离：每个用例独立执行，单个失败以 degraded 记录（run_one_case
    内部已 try/except），绝不让一个超时/异常取消其余用例。线程池 map 天然保序，
    HTTP target 的 httpx.Client 是线程安全的（独立连接），进程内 executor 的
    副作用由调用方保证（GoldenFollower/PromptAware 均无共享变更）。
    错误计数独立累加，A08 报告据此给 error_count。
    """
    if parallel_workers <= 0:
        return _parallel_metrics(parallel_workers=0,
                                 wall_ms=0.0, serial_wall_ms=0.0,
                                 serial_count=0, peak_rss_mb=None), \
               [run_one_case(c, target=target, executor=executor, run_id=run_id,
                             use_llm=use_llm, llm_cb=llm_cb) for c in cases]

    workers = min(int(parallel_workers), PARALLEL_MAX_WORKERS)
    errors: dict[str, str] = {}
    peak_rss_mb = _peak_rss_mb_snapshot()

    def _one(case: dict) -> dict:
        res = run_one_case(case, target=target, executor=executor, run_id=run_id,
                           use_llm=use_llm, llm_cb=llm_cb)
        if res.get("error"):
            errors[res.get("case_id", case.get("id", ""))] = res["error"]
        return res

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, cases))   # map 保持 golden 原始顺序
    wall_ms = round((time.perf_counter() - t0) * 1000, 1)
    peak_rss_mb = _peak_rss_mb_snapshot(peak_rss_mb)

    # A08 加速比需要真实串行基线：同数据集补跑一次串行墙钟（不计入结果，
    # 只作为对比参照）。失败隔离下两条路径都耗时，但串行基线仅测时，纯 CPU
    # executor/无网络时开销极小。
    serial_wall_ms = _serial_baseline_wall(
        cases, target=target, executor=executor, run_id=run_id,
        use_llm=use_llm, llm_cb=llm_cb)
    return (_parallel_metrics(parallel_workers=workers, wall_ms=wall_ms,
                              serial_wall_ms=serial_wall_ms,
                              serial_count=len(cases), peak_rss_mb=peak_rss_mb,
                              error_count=len(errors)), results)


def _serial_baseline_wall(
    cases: list[dict], *, target, executor, run_id, use_llm, llm_cb
) -> float:
    """串行基线墙钟（毫秒）。失败隔离保证单条失败不影响整体计时。"""
    t0 = time.perf_counter()
    for c in cases:
        run_one_case(c, target=target, executor=executor, run_id=run_id,
                     use_llm=use_llm, llm_cb=llm_cb)
    return round((time.perf_counter() - t0) * 1000, 1)


def _peak_rss_mb_snapshot(prev: float | None = None) -> float:
    """进程峰值 RSS（MB）。无 psutil 时返回上次值/0，绝不报假数据。"""
    try:
        import psutil
        p = psutil.Process()
        rss = p.memory_info().rss / (1024 * 1024)
        return round(max(rss, prev or 0.0), 1)
    except Exception:  # noqa: BLE001 —— 资源采集失败不阻断评测
        return round(prev or 0.0, 1)


def _parallel_metrics(*, parallel_workers: int, wall_ms: float,
                      serial_wall_ms: float, serial_count: int = 0,
                      peak_rss_mb: float | None, error_count: int = 0) -> dict:
    """A08 并行度量报告段。不达标/未测 -> measured=False，绝不伪造。

    speedup 只在确有两段实测墙钟且串行>0 时给出；其余字段照实给数但
    measured=False 标志整个并行段未达成验证目标。
    """
    measured = bool(parallel_workers > 0 and serial_wall_ms > 0.0 and wall_ms > 0.0)
    speedup = round(serial_wall_ms / wall_ms, 2) if measured else None
    return {
        "enabled": parallel_workers > 0,
        "workers": parallel_workers,
        "wall_ms": round(wall_ms, 1),
        "serial_wall_ms": round(serial_wall_ms, 1) if serial_wall_ms else None,
        "speedup": speedup,
        "error_count": error_count,
        "resource": {
            "peak_rss_mb": peak_rss_mb,
            "measured": peak_rss_mb is not None and peak_rss_mb > 0.0,
        },
        "measured": measured,
    }


# ---- 全量跑 + 报告 ----

def run_eval(
    *,
    target: RefundSystemHttpTarget | None = None,
    executor: Executor | None = None,
    limit: int = 0,
    use_llm: bool = False,
    llm_cb: Callable[[CandidateOutput, dict], dict] | None = None,
    report_path: str | Path | None = None,
    dataset_checksum: str | None = None,
    parallel_workers: int = 0,
) -> dict:
    """跑全量（或 limit 条）Golden，生成聚合 JSON 报告。

    A03/A08：
    - parallel_workers<=0 串行（兼容旧行为）；>0 用线程池并发，保留原始顺序；
    - 单个用例失败/超时只记该条 degraded，不取消其他任务（失败隔离）；
    - 报告 performance.parallel 记录并发模式、墙钟耗时、相较串行的加速比、
      错误数与进程峰值 RSS（资源占用）。串行基线在并行模式下同数据集补跑，
      保证 speedup 可复现。实测值不足（并发 walls==0 或串行基线失败）时
      measured=False，绝不伪造达标。
    """
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    cases = GOLDEN_CASES if not limit else GOLDEN_CASES[:limit]
    checksum_now = build_dataset_document()["checksum"]
    if dataset_checksum and dataset_checksum != checksum_now:
        raise ValueError(
            f"数据集校验和不匹配: 期望 {dataset_checksum} 实际 {checksum_now}")

    parallel_metrics, results = _run_cases(
        cases, target=target, executor=executor, run_id=run_id,
        use_llm=use_llm, llm_cb=llm_cb, parallel_workers=parallel_workers,
    )

    latencies: list[float] = []
    token_in_total = 0
    token_out_total = 0
    cost_total = 0.0
    _candidate_measured = False
    for res in results:
        latencies.append(res["execution"]["latency_ms"])
        cand = res.get("candidate") or {}
        usage = cand.get("usage") or {}
        token_in_total += int(usage.get("input_tokens") or 0)
        token_out_total += int(usage.get("output_tokens") or 0)
        cost_total += float(usage.get("cost") or 0.0)
        if usage.get("measured"):
            _candidate_measured = True

    n = len(results)
    passed = sum(1 for r in results if r["judge"].get("verdict") == "pass")
    failed = sum(1 for r in results if r["judge"].get("verdict") == "fail")
    degraded = n - passed - failed

    report = {
        "report_version": "1.0",
        "run_id": run_id,
        "created_at": _now_iso(),
        "environment": _env_report(),
        "dataset": {
            "version": DATASET_META["version"],
            "checksum": checksum_now,
            "case_count": len(cases),
            "full_count": len(GOLDEN_CASES),
            "limit": limit,
        },
        "summary": {
            "total": n,
            "passed": passed,
            "failed": failed,
            "degraded": degraded,
            "pass_rate": round(passed / n, 4) if n else 0.0,
        },
        "performance": {
            "latency_ms": _p50p95(latencies),
            # A08：并行化时延降低目标（速度、错误、资源三要素），串行时如实 disabled
            "parallel": parallel_metrics,
        },
        "cost": {
            "input_tokens_total": token_in_total,
            "output_tokens_total": token_out_total,
            "cost_total": cost_total,
            "measured": _candidate_measured,
        },
        "extensions": {
            "security": None,
            "intent": None,
            "voice": None,
            "coaching": None,
        },
        "trace": {
            "trace_id": run_id,        # case 级 trace 由 target/executor 生成
            "run_id": run_id,
        },
        "cases": results,
    }

    if report_path:
        p = Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        report["_report_file"] = str(p)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def save_default_report(report: dict) -> Path:
    """归档到 reports/run_id.json（默认落盘）。"""
    run_dir = REPORT_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = report["run_id"]
    p = run_dir / f"{run_id}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---- CLI ----

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_refund.runner",
        description="退赔系统 V1.0 Golden 评测（HTTP 黑盒）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 条（0=全部 15）")
    parser.add_argument("--judge", action="store_true",
                        help="启用附加 LLM judge（未配置自动 fallback）")
    parser.add_argument("--base-url", default=None,
                        help="被测系统 base_url（默认 REFUND_SYSTEM_BASE_URL 或 %s）"
                             % "http://127.0.0.1:8000")
    parser.add_argument("--report", default=None,
                        help="报告输出路径（默认 reports/<run_id>.json）")
    parser.add_argument("--checksum", default=None,
                        help="数据集期望校验和（校验不匹配即失败）")
    parser.add_argument("--parallel", type=int, default=0, metavar="N",
                        help="并行 worker 数（0=串行；N>0 并发执行并报告 A08 加速比/资源）")
    args = parser.parse_args(argv)

    from .llm_judge_client import run_llm_judge

    target = RefundSystemHttpTarget(base_url=args.base_url)
    report = run_eval(
        target=target,
        limit=args.limit,
        use_llm=args.judge,
        llm_cb=run_llm_judge,
        dataset_checksum=args.checksum,
        parallel_workers=args.parallel,
    )
    out = save_default_report(report)
    print(f"run_id={report['run_id']}")
    print(f"结果: {report['summary']['passed']}/{report['summary']['total']} passed, "
          f"failed={report['summary']['failed']}, degraded={report['summary']['degraded']}")
    print(f"报告: {out}")
    # degraded（目标不可用/超时）同样视为未达标：环境不可用不能绿灯
    if report["summary"]["degraded"]:
        print("注意: 存在 degraded（目标不可用/超时），本次不算评测通过")
        return 2
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())