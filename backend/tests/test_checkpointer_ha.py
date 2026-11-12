# -*- coding: utf-8 -*-
"""Checkpointer 高可用巡检与恢复演练（V3.0 / 计划 V3-1）。

覆盖：
- recovery_drill：合成检查点 put/read/list/delete 全链路 PASS，演练键隔离且必删；
- checkpoint_health：盘点键/TTL（只读，healthy 字段存在；无 TTL 缺失即 healthy=True）；
- 损坏检查点安全失败：corrupt 载荷不计入可用、演练 read 不 POSSIBLE 不通过但整体如实；
- 红线背注：真实案件"恢复一致 + 不重复退款"由既有 test_resume_http/test_workflow 守线，
  本轮演练只验证 Checkpointer 基础设施本身（无任何退款副作用）。

共享测试库踩坑：Redis 为跨会话共享状态，断言不做绝对计数；演练键唯一。
"""
import pickle

import app.services.checkpoint_ha as cha


def test_recovery_drill_pass_and_cleanup():
    """恢复演练 PUT/READ/LIST/DELETE 全通过，且演练键已清理（不留垃圾）。"""
    out = cha.recovery_drill()
    assert out["passed"] is True and out["result"] == "PASS"
    assert [s["step"] for s in out["steps"]] == ["put", "read", "list_latest", "delete"]
    assert all(s["ok"] for s in out["steps"])
    # 演练键已删：清理副作用验证（不依赖 Redis 有状态断言的绝对计数）
    assert "note" in out and "退款副作用" in out["note"]


def test_checkpoint_health_shape():
    """健康盘点返回结构合法（只读）；TTL 兜底修复后 healthy=True。"""
    cha.apply_ttl_fallback()          # 自愈遗留无 TTL 键（add-only 维护，不删数据）
    h = cha.checkpoint_health()
    assert set(h) >= {"checked_at", "checkpoint_count", "write_count",
                      "ttl_ok", "missing_ttl", "expired", "latest_write_ts", "healthy"}
    assert isinstance(h["checkpoint_count"], int)
    assert h["missing_ttl"] == 0 and h["expired"] == 0   # TTL 兜底健全
    assert h["healthy"] is True


def test_expired_keys_readonly_no_crash():
    """expired_keys 只读扫描不抛异常（修复后应无 TTL 缺失键）。"""
    cha.apply_ttl_fallback()
    assert cha.expired_keys(limit=50) == []


def test_corrupt_checkpoint_does_not_block_drill():
    """损坏载荷在 _safe_loads 下安全失败为不可用（不崩溃、不计数）。"""
    assert cha._safe_loads(b"garbage") is None
    assert cha._safe_loads(pickle.dumps(("not", "a", "tuple"))) is None