#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""可信运行积攒驾驶舱（V3.0 前置补齐 / V3-2）。

在真实使用（定时/变更触发回归）中积攒 ≥10 次可信评测运行 + 稳定基线批准。
只读时打印积分与缺口；`--approve` 在阈值达标且存在未批准基线时执行真实批准
（写 eval_baselines + 审计事件）。

可信判定（release_gate._is_trusted_run）：COMPLETED + cost.measured + summary 完整
+ trigger_source 属真实使用来源（schedule/manual/code/...）。测试专用来源不计数。
红线：绝不刷数不造假 —— dev 库无真实运行即如实打印 PENDING / 缺口。

用法（backend 目录下）：
    PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe scripts/accumulate_trusted_runs.py
    PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe scripts/accumulate_trusted_runs.py --approve
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, _BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ACCESS_TOKEN = "trusted-runs-accumulator"     # 脚本身份标记（审计可见）


def _session():
    from sqlalchemy.orm import Session
    from app.infrastructure.db import engine
    return Session(engine)


def main() -> int:
    approve = "--approve" in sys.argv[1:]
    from app.infrastructure.audit import write_event
    from app.services import release_gate as rg

    with _session() as db:
        health = rg.trusted_runs_health(db)
        print("=" * 64)
        print("可信运行积攒驾驶舱  (目标 {} 次 + 稳定基线批准)".format(health["target"]))
        print("=" * 64)
        print("state          : {}".format(health["state"]))
        print("trusted_runs   : {} (缺口 {})".format(health["trusted_runs"],
                                                     health["gap"]))
        print("by_suite       : {}".format(health["by_suite"]))
        print("baseline_approved: {}".format(health["baseline_approved"]))
        print("note           : {}".format(health["note"]))
        if health["run_ids"]:
            print("recent_run_ids :")
            for rid in health["run_ids"]:
                print("  - {}".format(rid))
        if not approve:
            print("-" * 64)
            print("未传 --approve，跳过批准动作（只读）。")
            return 0
        res = rg.maybe_approve_trusted_baseline(db, actor_id=ACCESS_TOKEN)
        print("-" * 64)
        print("approve        : {} approved={}".format(res["state"],
                                                       len(res["approved"])))
        print("approve_note   : {}".format(res["note"]))
        for bl in res["approved"]:
            write_event(db, actor_id=ACCESS_TOKEN, case_id=None,
                        action="GATE_BASELINE_APPROVED",
                        payload={"suite_id": bl["suite_id"], "run_id": bl["run_id"],
                                 "note": "trusted-run-accumulator"})
            print("  approved baseline: suite={} run={}".format(bl["suite_id"],
                                                                bl["run_id"]))
        db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())