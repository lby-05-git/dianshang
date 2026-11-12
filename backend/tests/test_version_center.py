# -*- coding: utf-8 -*-
"""V3.0 V3-3 版本注册中心（7 类实体版本）。

覆盖（service + API）：
- register：DRAFT + 内容 sha256 指纹；重复 (entity, name, version) 拒绝；非法实体类型拒；
- approve/reject：状态只能 DRAFT/CANDIDATE 流转；批准后留 approver/approved_at；
- rollback：仅已批准版本可回滚，目标必须是已批准历史版本；只留痕不改业务语义；
- history / latest_approved：按时间倒序、最近已批准生效；
- 每次状态变更落 DomainEvent 审计（append-only）；
- API RBAC：leader 可管理；客服 403。

共享测试库踩坑：整库复用；只做相对/键断言，不数全局行数。
"""
import uuid

import pytest

from app.domain import models as M
from app.services import version_center as vc
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login


def _rid(s):
    return f"reg-{uuid.uuid4().hex[:10]}"


def test_register_and_history():
    s = SessionTest()
    try:
        name = f"prompt-{uuid.uuid4().hex[:6]}"
        item = vc.register(s, entity_type="prompt", entity_name=name,
                           version="v1", content="refund prompt body",
                           changelog="init", creator_id="u-1")
        assert item["status"] == "DRAFT"
        assert item["content_sha256"]  # 内容指纹已算
        assert item["payload"] == {}

        h = vc.history(s, entity_type="prompt", entity_name=name)
        assert h["latest_approved"] is None
        assert [x["version"] for x in h["versions"]] == ["v1"]
    finally:
        s.close()


def test_register_duplicate_rejected():
    s = SessionTest()
    try:
        name = f"prompt-{uuid.uuid4().hex[:6]}"
        vc.register(s, entity_type="prompt", entity_name=name, version="v1")
        with pytest.raises(ValueError):
            vc.register(s, entity_type="prompt", entity_name=name, version="v1")
    finally:
        s.close()


def test_register_invalid_entity_type():
    s = SessionTest()
    try:
        with pytest.raises(ValueError):
            vc.register(s, entity_type="nope", entity_name="x", version="v1")
    finally:
        s.close()


def test_approve_reject_flow():
    s = SessionTest()
    try:
        name = f"tool-{uuid.uuid4().hex[:6]}"
        r1 = vc.register(s, entity_type="tool", entity_name=name, version="v1")
        # approve
        ok = vc.approve(s, reg_id=r1["id"], approver_id="u-leader")
        assert ok["status"] == "APPROVED"
        assert ok["approver_id"] == "u-leader" and ok["approved_at"]
        # 已批准不可再批准/否决
        with pytest.raises(ValueError):
            vc.approve(s, reg_id=r1["id"])
        with pytest.raises(ValueError):
            vc.reject(s, reg_id=r1["id"])
    finally:
        s.close()


def test_rollback_only_to_approved_history():
    s = SessionTest()
    try:
        name = f"policy-{uuid.uuid4().hex[:6]}"
        v1 = vc.register(s, entity_type="security_policy", entity_name=name, version="v1")
        v1 = vc.approve(s, reg_id=v1["id"], approver_id="u-leader")
        v2 = vc.register(s, entity_type="security_policy", entity_name=name, version="v2")
        # v2 未批准 -> 不可回滚（reg 必须已批准）
        with pytest.raises(ValueError):
            vc.rollback(s, reg_id=v2["id"], target_version="v1")
        v2 = vc.approve(s, reg_id=v2["id"], approver_id="u-leader")
        # 回滚到当前版本 -> 拒绝
        with pytest.raises(ValueError):
            vc.rollback(s, reg_id=v2["id"], target_version="v2")
        # 回滚到不存在的版本 -> 拒绝
        with pytest.raises(ValueError):
            vc.rollback(s, reg_id=v2["id"], target_version="nope")
        # 正常回滚
        r = vc.rollback(s, reg_id=v2["id"], target_version="v1", approver_id="u-leader")
        assert r["status"] == "ROLLED_BACK" and r["rollback_to"] == "v1"
        assert r["approver_id"] == "u-leader"
        # 回滚只是标记，latest_approved 仍是最新已批准（v1 生效）
        latest = vc.latest_approved(s, entity_type="security_policy", entity_name=name)
        assert latest["version"] == "v1"
    finally:
        s.close()


def test_latest_approved_picks_recent_approved():
    import time
    s = SessionTest()
    try:
        name = f"model-{uuid.uuid4().hex[:6]}"
        r1 = vc.register(s, entity_type="model", entity_name=name, version="r1")
        time.sleep(1.1)          # created_at 秒级粒度：拉开时间戳避免同秒二义
        r2 = vc.register(s, entity_type="model", entity_name=name, version="r2")
        vc.approve(s, reg_id=r1["id"])
        assert vc.latest_approved(s, entity_type="model", entity_name=name)["version"] == "r1"
        time.sleep(1.1)
        vc.approve(s, reg_id=r2["id"])
        assert vc.latest_approved(s, entity_type="model", entity_name=name)["version"] == "r2"
    finally:
        s.close()


def test_state_changes_write_domain_audit():
    s = SessionTest()
    try:
        name = f"security-{uuid.uuid4().hex[:6]}"
        before = s.query(M.DomainEvent).count()
        r = vc.register(s, entity_type="security_policy", entity_name=name, version="v1")
        vc.approve(s, reg_id=r["id"], approver_id="u-leader")
        after = s.query(M.DomainEvent).count()
        assert after >= before + 2     # REGISTERED + APPROVED
        acts = [e.action for e in s.query(M.DomainEvent)
                .order_by(M.DomainEvent.created_at.desc()).limit(2).all()]
        assert vc.ACT_VERSION_APPROVED in acts
    finally:
        s.close()


# ---------- API + RBAC ----------

def _api_h(client, username):
    return _auth_headers(login(client, username))


def test_api_versions_rbac(client):
    """客服不可管理版本（403）；leader 全流程可用。"""
    # 客服：list 可读，create 403
    r = client.get("/api/v1/governance/versions",
                   headers=_api_h(client, "customer_service1"))
    assert r.status_code == 200
    r = client.post("/api/v1/governance/versions",
                    json={"entity_type": "prompt", "entity_name": "p-x",
                          "version": "v1"},
                    headers=_api_h(client, "customer_service1"))
    assert r.status_code == 403
    # leader：create -> approve -> history
    headers = _api_h(client, "leader1")
    name = f"api-{uuid.uuid4().hex[:6]}"
    r = client.post("/api/v1/governance/versions",
                    json={"entity_type": "prompt", "entity_name": name,
                          "version": "v1", "changelog": "api"},
                    headers=headers)
    assert r.status_code == 201, r.text
    rid = r.json()["item"]["id"]
    r = client.post(f"/api/v1/governance/versions/{rid}/approve", headers=headers)
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "APPROVED"
    r = client.get(f"/api/v1/governance/versions/history?entity_type=prompt&entity_name={name}",
                   headers=_api_h(client, "customer_service1"))
    assert r.status_code == 200
    assert r.json()["item"]["latest_approved"]["version"] == "v1"