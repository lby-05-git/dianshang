# -*- coding: utf-8 -*-
"""M21 security/intent/voice 插件契约测试（V3.0 持续评测治理）。

覆盖：
- S1 能力注册表默认态：核心能力 implemented；预留能力 unavailable（不伪 pass）。
- S2 插件可注册新能力/新 Metric，只扩展注册表，不触碰核心表/核心字段。
- S3 unimplemented_metrics：能力未实现时其 metric 标记为未测（不当作达标）。
- S4 target_declaration：audio/streaming 未实现时 unavailable。
- S5 预留标签用例在 Worker 中始终 SKIPPED_UNSUPPORTED（voice/intent/security/coaching）。
- S6 API /capabilities 返回 unimplemented 列表；RBAC 门禁。

契约红线：只注册能力/度量声明，绝不修改核心表结构、不伪造可用能力通过。
"""
import json
import uuid

import pytest

from app.domain import models
from app.services import eval_center as ec
from app.worker.eval_processor import EvalRunConsumer
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login

from eval_refund.capabilities import (
    capability_table,
    metric_table,
    register_capability,
    register_metric,
    target_declaration,
    unimplemented_metrics,
)


def _api_h(client, username):
    return _auth_headers(login(client, username))


# ---- S1 能力注册表 ----

def test_default_capability_table():
    """V1.0 修复后：四能力由 capability_plugin 声明为已实现（available）。

    注册表默认态仍是 unavailable（未装插件/禁用插件时不伪 pass）；基线安装插件
    后如实翻转为 available——这正是 plan 明示"追加 G151+ 批次"修复的目标。
    """
    tbl = capability_table()
    for cap in ("security", "intent", "voice", "coaching"):
        meta = tbl[cap]
        # 已实现（插件安装后翻转；禁用的进程/测试仍回落到 unavailable）
        assert meta["availability"] == "available", f"{cap} 应为 available（插件已声明实现）"
        assert meta["implemented"] is True, f"{cap} 应 implemented=True"
    # voice 声明 audio/streaming
    assert tbl["voice"]["streaming"] is True


# ---- S2 插件注册（不触碰核心表）----

def test_plugin_registers_capability_and_metric():
    # 用唯一能力名，避免污染默认预留能力表
    name = f"plugin_test_{uuid.uuid4().hex[:6]}"
    register_capability(name, availability="declared", modality="markup",
                        streaming=True, metric_names=(f"{name}_metric",))
    tbl = capability_table()
    assert name in tbl
    assert tbl[name]["availability"] == "declared"
    assert tbl[name]["streaming"] is True
    mt = metric_table()
    assert f"{name}_metric" in mt
    # 未实现（declared 非 available）-> 其 metric 归为未测
    assert f"{name}_metric" in unimplemented_metrics()


# ---- S3 unimplemented metrics ----

def test_unimplemented_metrics_lists_declared_but_unavailable():
    name = f"probe_{uuid.uuid4().hex[:6]}"
    # 绑定一个未声明/未知能力 -> 该 metric 仍应归入"未测"
    register_metric(name, capability="not_a_real_cap", dim_type="score", source="probe")
    assert name in metric_table()
    assert name in unimplemented_metrics()
    # 声明的四能力已实现 -> 绑定到它们的 metric 不应被误标未测
    for cap in ("security", "intent", "voice", "coaching"):
        assert capability_table()[cap]["implemented"] is True


# ---- S4 target declaration ----

def test_target_declaration_audio_streaming():
    decl = target_declaration()  # 默认未声明 audio/streaming
    assert decl["audio"]["implemented"] is False
    assert decl["streaming"]["implemented"] is False
    decl2 = target_declaration(audio=True, streaming=True)
    assert decl2["audio"]["implemented"] is True
    assert decl2["streaming"]["implemented"] is True


# ---- S5 Worker 预留标签用例始终 SKIPPED_UNSUPPORTED ----

def test_worker_reserved_cases_skipped_unsupported():
    s = SessionTest()
    suite = ec.create_suite_from_golden(
        s, name=f"m21-{uuid.uuid4().hex[:8]}", changelog="M21")
    run = ec.create_run(s, run_id=f"run-{uuid.uuid4().hex[:10]}", name="M21 reserved",
                        suite=s.get(models.EvalSuite, suite["id"]),
                        created_by="u-test", config={"mode": "in_process"})
    EvalRunConsumer(session_factory=SessionTest).process(
        "m21res", {"case_id": run.run_id, "run_id": run.run_id,
                   "suite_id": suite["id"],
                   "config": json.dumps({"mode": "in_process", "limit": 0})})
    got = ec.get_run(SessionTest(), run.run_id)
    assert got["status"] == "COMPLETED"
    results = ec.list_run_results(SessionTest(), run.run_id)
    by_id = {r["case_id"]: r for r in results}
    # 数据集尾部的 8 条预留标签用例（G143..G150）must 全 SKIPPED_UNSUPPORTED
    for cid in (f"G{i:02d}" for i in range(143, 151)):
        assert by_id[cid]["verdict"] == "SKIPPED_UNSUPPORTED", cid
    # 不把预留能力当通过
    assert got["summary"]["passed"] < available_lower_bound()
    # 明确有 skipped_unsupported 计数
    assert got["summary"].get("skipped_unsupported", 0) >= 8


def available_lower_bound() -> int:
    # 150 条全量中有 8 条预留 -> 通过的用例数必然 < 150-10（保守）
    return 142


# ---- S6 API ----

def test_api_capabilities_contract(client):
    h = _api_h(client, "leader1")
    r = client.get("/api/v1/evaluation-center/capabilities", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "security" in body["capabilities"]
    # V1.0 修复：插件已声明四能力为已实现 -> /capabilities 如实返回 available
    assert body["capabilities"]["voice"]["implemented"] is True
    assert body["capabilities"]["voice"]["availability"] == "available"
    assert body["capabilities"]["security"]["implemented"] is True
    # 由此 unimplemented 不再包含已实现能力
    assert "security" not in body["unimplemented"]
    assert "voice" not in body["unimplemented"]
    assert isinstance(body["metrics"], dict)


def test_api_capabilities_metrics_registered(client):
    """修复后：/capabilities 的 metrics 应含四能力 compute 注册（非空实现）。"""
    from eval_refund import judge as judgment
    for metric in ("security.redline", "intent.confusion", "voice.asr",
                   "coaching.custom"):
        m = judgment.METRICS.get(metric)
        assert m is not None, f"{metric} 应已注册"
        assert m["registered"] is True, f"{metric} 应 registered"
        assert m["group"] in ("security", "intent", "voice", "coaching")
        assert m["compute"] is not None, f"{metric} 应有真实 compute（不得空实现冒充）"


def test_api_rbac_cs_cannot_capabilities(client):
    h = _api_h(client, "customer_service1")
    r = client.get("/api/v1/evaluation-center/capabilities", headers=h)
    assert r.status_code == 403