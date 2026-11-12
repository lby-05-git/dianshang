# -*- coding: utf-8 -*-
"""生产 Trace 漂移监测（V3.2 -评测：低比例脱敏抽样 -> 漂移指标/告警）。

覆盖：
- 脱敏：订单号/手机号/uuid 遮蔽、长文本截断（绝不落明文 PII）；
- 抽样比例合法性：0<ratio≤0.5，确定性（同输入同输出）；
- 样本不足 -> 如实 not_measured（永不当作达标）；
- 窗口聚合 + 与前窗对比 -> 漂移落 eval_drift_indicators + DRIFT 告警；
- 读摘要形状。

共享测试库踩坑：整库复用；用 uuid 意图值隔离 metric_key，不做绝对行数断言。
"""
import uuid

import pytest

from app.domain import models as M
from app.services import drift_monitor as dm
from tests.conftest import SessionTest


def _samples(n, *, intent, decision="APPROVED", human=None, err=None,
             text="订单 ORD-1001 手机号 13800000000 我要退款"):
    # 每条样本给唯一文本：哈希分桶才对样本做比例抽样（同文哈希全同会全抽/全不抽）
    return [dm.DriftSample(input_text=f"{text} #{i}", intent=intent,
                           decision=decision, human_override=human,
                           model_error=err, source="production")
            for i in range(n)]


def test_mask_pii_hides_sensitive_fields():
    s = dm.DriftSample(input_text="订单 ORD-1001 手机号 13800009999 我要退款 uuid a1b2c3d4",
                       intent="RFX", decision="APPROVED")
    m = dm.mask_pii(s)
    assert "ORD-1001" not in m["input_text"]
    assert "13800009999" not in m["input_text"]
    assert "ORD-****" in m["input_text"]
    assert "138****0000" in m["input_text"]
    assert len(m["input_text"]) <= 500
    # 意图/决策保留（非 PII），供指标聚合
    assert m["intent"] == "RFX" and m["decision"] == "APPROVED"


def test_sample_ratio_legality_and_determinism():
    with pytest.raises(ValueError):
        dm.sample_production(_samples(20, intent="X"), ratio=0.0)
    with pytest.raises(ValueError):
        dm.sample_production(_samples(20, intent="X"), ratio=0.51)
    pool = _samples(500, intent="ABC")
    a = dm.sample_production(pool, ratio=0.1)
    b = dm.sample_production(pool, ratio=0.1)
    assert [x.input_text for x in a] == [x.input_text for x in b]   # 确定性
    assert 0 < len(a) < 200                               # 低比例收敛（10% 远不达全量）
    # 半量红线：绝不>半样本（确定性收敛）
    half = dm.sample_production(pool, ratio=0.5)
    assert 0 < len(half) < 500


def test_sweep_insufficient_samples_not_measured():
    s = SessionTest()
    try:
        out = dm.sweep(s, _samples(3, intent="LOW"), ratio=0.5)
        assert out["not_measured"] is True
        assert out["records"] == 0
        assert "不足" in out["note"]
    finally:
        s.close()


def test_sweep_records_and_alerts_on_drift():
    s = SessionTest()
    try:
        tag = uuid.uuid4().hex[:8]           # 隔离 metric_key，避免跨测试串扰
        # 第一窗：意图 baseA 占比 ~0.5
        pooled = _samples(120, intent=f"baseA{tag}")
        pooled += _samples(120, intent=f"baseB{tag}")
        out1 = dm.sweep(s, pooled, ratio=0.5)
        assert out1["not_measured"] is False and out1["records"] > 0
        # 第二窗：意图 baseA 占比 ~0.9 -> 相对基线漂移
        pooled2 = _samples(270, intent=f"baseA{tag}")
        pooled2 += _samples(30, intent=f"baseB{tag}")
        out2 = dm.sweep(s, pooled2, ratio=0.5)
        key = f"prod/intent_basea{tag.lower()}"
        assert out2["records"] > 0
        row = (s.query(M.EvalDriftIndicator)
               .filter_by(metric_key=key, run_id=out2["run_id"]).first())
        assert row is not None and row.drifted is True     # 漂移信号已落
        assert "sanitized" in (row.detail or {})
        # detail 里绝无明文订单/手机号
        detail = row.detail or {}
        assert "ORD-" not in str(detail) and "138" not in str(detail)
    finally:
        s.close()


def test_production_drift_summary_shape():
    s = SessionTest()
    try:
        out = dm.production_drift_summary(s)
        assert isinstance(out["items"], list)
        assert "note" in out and "PII" in out["note"]
        for it in out["items"]:
            assert it["metric_key"].startswith("prod/")
    finally:
        s.close()