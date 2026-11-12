# -*- coding: utf-8 -*-
"""V3.0 V3-3 安全服务故障 Fail-Closed。

覆盖：
- 默认全健康 -> 高风险/普通查询均 ALLOW（零行为变化）；
- 注入 security_gateway 故障 -> 高风险退款操作 Fail-Closed DENY（绝不因故障放开退款）；
- 注入 ner_dlp 故障 -> 普通查询按批准降级策略（mask_only）返回 DEGRADE 且必须已脱敏；
  无批准策略（默认 deny）-> DENY；非法模式一律 deny；
- 恢复健康 -> 自愈 ALLOW；
- DENY/DEGRADE 落 DomainEvent 审计（append-only，只留痕）。

注意：cause_health 改全局探针，测试 finally 必须 reset_health()，避免串扰其他测试。
"""
import pytest

from app.domain import models as M
from app.services import fail_closed as fc
from tests.conftest import SessionTest


def _clear_degrade_config(s) -> None:
    """清掉共享库里可能残留的降级策略配置，保证用例隔离。"""
    s.query(M.SystemConfig).filter_by(cfg_key=fc.DEGRADE_CONFIG_KEY).delete()
    s.commit()


def test_healthy_services_allow_high_risk_and_query():
    s = SessionTest()
    try:
        fc.reset_health()
        hr = fc.guard_high_risk(s, persist=False)
        assert hr.action == "ALLOW" and hr.healthy is True
        q = fc.guard_query(s, persist=False)
        assert q.action == "ALLOW" and q.healthy is True
    finally:
        fc.reset_health()
        s.close()


def test_gateway_failure_denies_high_risk():
    """输入网关故障 -> 高风险退款操作 Fail-Closed DENY（资金面绝不放开）。"""
    s = SessionTest()
    try:
        fc.cause_health(fc.SVC_SECURITY_GATEWAY, False)
        hr = fc.guard_high_risk(s, persist=True)
        assert hr.action == "DENY"
        assert "Fail-Closed" in hr.reason
    finally:
        fc.reset_health()
        s.close()


def test_ner_failure_degrades_by_approved_policy_masked_only():
    """已批准降级策略 mask_only -> 普通查询 DEGRADE 且只回脱敏结果。"""
    s = SessionTest()
    try:
        _clear_degrade_config(s)
        fc.cause_health(fc.SVC_NER_DLP, False)
        row = M.SystemConfig(cfg_key=fc.DEGRADE_CONFIG_KEY,
                             cfg_value={"mode": fc.MODE_MASK_ONLY})
        s.add(row)
        s.commit()
        q = fc.guard_query(s, service=fc.SVC_NER_DLP, persist=True)
        assert q.action == "DEGRADE"
        assert q.mask_only is True
        assert q.degrade_mode == fc.MODE_MASK_ONLY
    finally:
        _clear_degrade_config(s)
        fc.reset_health()
        s.close()


def test_ner_failure_without_approved_policy_denies():
    """无批准降级策略（默认）-> 普通查询 DENY（零信任默认拒绝）。"""
    s = SessionTest()
    try:
        _clear_degrade_config(s)
        fc.cause_health(fc.SVC_NER_DLP, False)
        q = fc.guard_query(s, service=fc.SVC_NER_DLP, persist=False)
        assert q.action == "DENY"
        assert "无已批准降级策略" in q.reason
    finally:
        fc.reset_health()
        s.close()


def test_ner_failure_invalid_mode_denies():
    """非法降级模式一律拒绝（绝不回明文）。"""
    s = SessionTest()
    try:
        _clear_degrade_config(s)
        fc.cause_health(fc.SVC_NER_DLP, False)
        s.add(M.SystemConfig(cfg_key=fc.DEGRADE_CONFIG_KEY,
                             cfg_value={"mode": "echo_plaintext"}))   # 非法模式
        s.commit()
        q = fc.guard_query(s, service=fc.SVC_NER_DLP, persist=False)
        assert q.action == "DENY"
    finally:
        _clear_degrade_config(s)
        fc.reset_health()
        s.close()


def test_health_recovers_and_allows_again():
    """恢复健康 -> 自愈 ALLOW。"""
    s = SessionTest()
    try:
        fc.cause_health(fc.SVC_SECURITY_GATEWAY, False)
        assert fc.guard_high_risk(s, persist=False).action == "DENY"
        fc.cause_health(fc.SVC_SECURITY_GATEWAY, True)
        assert fc.guard_high_risk(s, persist=False).action == "ALLOW"
    finally:
        fc.reset_health()
        s.close()


def test_deny_writes_domain_audit():
    s = SessionTest()
    try:
        _clear_degrade_config(s)
        fc.cause_health(fc.SVC_LLM_CRITIC, False)
        before = s.query(M.DomainEvent).count()
        fc.evaluate_fail_closed(s, operation=fc.OP_QUERY,
                                service=fc.SVC_LLM_CRITIC, persist=True)
        after = s.query(M.DomainEvent).count()
        assert after >= before + 1
        # 同秒 created_at 二义：按键过滤，不依赖"最新一行"归属
        rows = (s.query(M.DomainEvent)
                .filter_by(action="SECURITY_FAIL_CLOSED")
                .order_by(M.DomainEvent.created_at.desc())
                .all())
        mine = [r for r in rows if (r.payload or {}).get("service") == fc.SVC_LLM_CRITIC]
        assert mine, "应存在 llm_critic 的 Fail-Closed 审计"
        assert (mine[0].payload or {}).get("operation") == fc.OP_QUERY
    finally:
        fc.reset_health()
        s.close()


def test_unknown_service_rejected():
    with pytest.raises(ValueError):
        fc.cause_health("nope-service", False)
    with pytest.raises(ValueError):
        fc.evaluate_fail_closed(None, operation=fc.OP_QUERY, service="nope")