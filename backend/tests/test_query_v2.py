# -*- coding: utf-8 -*-
"""Task #2/#3 验证：高级检索底座(query_service) + data_scope 守卫 + LEADER 权限。

覆盖（3.3.2 高级检索 / 3.2.2 权限模型 / 3.3.7 审计导出管控）：
  - 多条件组合检索 / 关键词 / 金额区间 / 原因码命中
  - data_scope：self 只见本人、team 见同班组、all 见全部
  - LEADER+ 可导出、客服不可导出（403）
  - 筛选器保存 / 列表 / 删除
  - 非法排序字段回退 created_at（防 ORDER BY 注入）
"""
import uuid

from tests.conftest import SessionTest

from app.domain.models import (
    DECISION_PENDING,
    RefundCase,
    User,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client, username: str, password: str = "123456") -> str:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_case(session, *, username, amount="100.00", channel="APP",
               reason_codes=None, status="RUNNING", decision=DECISION_PENDING,
               order_no=None):
    """在测试库直接插入退款工单（用种子账号 submitter_id，避免依赖建单接口）。"""
    u = session.query(User).filter_by(username=username).first()
    c = RefundCase(
        order_id=f"ORD-Q-{uuid.uuid4().hex[:8]}",
        order_no=order_no,
        channel=channel,
        requested_amount=amount,
        reason_codes=reason_codes,
        submitter_id=u.id,
        current_status=status,
        final_decision=decision,
    )
    session.add(c)
    session.commit()
    return c.id


# ---- 高级检索：关键词 / 金额区间 / 原因码 / 组合 ----
def test_search_keyword_matches_reason_and_order(client):
    """用唯一 order_no 做关键词，避免历史测试数据污染（测试库跨用例共享）。"""
    cs = _login(client, "customer_service1")
    tok = _auth(cs)
    tag = uuid.uuid4().hex[:8]
    with SessionTest() as s:
        _seed_case(s, username="customer_service1", amount="88.00",
                   reason_codes=["QUALITY"], order_no=f"QN-KEY-{tag}-A")
        _seed_case(s, username="customer_service1", amount="88.00",
                   reason_codes=["QUALITY"], order_no=f"QN-KEY-{tag}-B")

    r = client.get("/api/v1/search/cases", params={"keyword": f"QN-KEY-{tag}", "size": 50},
                   headers=tok)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert {it["order_no"] for it in body["items"]} == {f"QN-KEY-{tag}-A", f"QN-KEY-{tag}-B"}


def test_search_amount_range(client):
    """金额区间：用唯一 order_no 圈定本次查询，避免命中历史案例。"""
    cs = _login(client, "customer_service1")
    tok = _auth(cs)
    tag = uuid.uuid4().hex[:8]
    with SessionTest() as s:
        _seed_case(s, username="customer_service1", amount="50.00", order_no=f"AMT-{tag}-LOW")
        _seed_case(s, username="customer_service1", amount="300.00", order_no=f"AMT-{tag}-MID")
        _seed_case(s, username="customer_service1", amount="900.00", order_no=f"AMT-{tag}-HIGH")

    r = client.get("/api/v1/search/cases",
                   params={"keyword": f"AMT-{tag}", "amount_min": "100", "amount_max": "500",
                           "size": 50},
                   headers=tok)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["order_no"] == f"AMT-{tag}-MID"
    assert float(r.json()["items"][0]["requested_amount"]) == 300.0


def test_search_reason_code_hits_json(client):
    """原因码 JSON 命中：仅返回含该原因码的工单（用唯一 order_no 圈定范围）。"""
    cs = _login(client, "customer_service1")
    tok = _auth(cs)
    tag = uuid.uuid4().hex[:8]
    with SessionTest() as s:
        _seed_case(s, username="customer_service1", order_no=f"RC-{tag}-D", reason_codes=["DELAY"])
        _seed_case(s, username="customer_service1", order_no=f"RC-{tag}-Q", reason_codes=["QUALITY"])

    r = client.get("/api/v1/search/cases",
                   params={"keyword": f"RC-{tag}", "reason_code": "DELAY", "size": 50},
                   headers=tok)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["order_no"] == f"RC-{tag}-D"


# ---- data_scope 守卫 ----
def test_data_scope_self_only_sees_own(client):
    """客服 data_scope=self：只能检索到本人提交的工单。"""
    cs = _login(client, "customer_service1")
    tok = _auth(cs)
    tag = uuid.uuid4().hex[:8]
    with SessionTest() as s:
        _seed_case(s, username="customer_service1", amount="10.00", order_no=f"SELF-{tag}-A")
        _seed_case(s, username="supervisor1", amount="10.00", order_no=f"SELF-{tag}-B")

    r = client.get("/api/v1/search/cases", params={"keyword": f"SELF-{tag}", "size": 50}, headers=tok)
    assert r.status_code == 200, r.text
    orders = [it["order_no"] for it in r.json()["items"]]
    assert f"SELF-{tag}-A" in orders
    assert f"SELF-{tag}-B" not in orders  # 本人看不到他人（self -> submitter/assignee）


def test_data_scope_team_sees_same_team(client):
    """班组长(data_scope=team)能看到同班组(supervisor1 >= leader1)成员的单，客服单不在内。"""
    ld = _login(client, "leader1")
    tok = _auth(ld)
    tag = uuid.uuid4().hex[:8]
    with SessionTest() as s:
        _seed_case(s, username="supervisor1", amount="10.00", order_no=f"TEAM-{tag}-IN")
        _seed_case(s, username="customer_service1", amount="10.00", order_no=f"TEAM-{tag}-OUT")

    r = client.get("/api/v1/search/cases", params={"keyword": f"TEAM-{tag}", "size": 50}, headers=tok)
    assert r.status_code == 200, r.text
    orders = [it["order_no"] for it in r.json()["items"]]
    assert f"TEAM-{tag}-IN" in orders      # 同班组可见
    assert f"TEAM-{tag}-OUT" not in orders  # customer_service1 不在 team-demo


def test_data_scope_admin_sees_all(client):
    """管理员(data_scope=all)：能检索到包括客服单在内的所有工单。"""
    ad = _login(client, "admin1")
    tok = _auth(ad)
    tag = uuid.uuid4().hex[:8]
    with SessionTest() as s:
        _seed_case(s, username="customer_service1", amount="10.00", order_no=f"ADMIN-{tag}-ALL")

    r = client.get("/api/v1/search/cases", params={"keyword": f"ADMIN-{tag}", "size": 50},
                   headers=tok)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1


# ---- 导出权限 ----
def test_export_requires_leader(client):
    cs = _login(client, "customer_service1")
    r = client.get("/api/v1/search/export", params={"size": 50}, headers=_auth(cs))
    assert r.status_code == 403


def test_export_leader_ok(client):
    ld = _login(client, "leader1")
    r = client.get("/api/v1/search/export", params={"size": 50}, headers=_auth(ld))
    assert r.status_code == 200
    assert r.text.startswith("case_id")  # CSV 表头


# ---- 筛选器保存 / 读取 / 删除 ----
def test_saved_search_crud(client):
    cs = _login(client, "customer_service1")
    tok = _auth(cs)
    name = f"筛选-{uuid.uuid4().hex[:6]}"
    r = client.post("/api/v1/search/saved",
                    json={"name": name, "filters": {"channel": "APP", "amount_min": "1"}},
                    headers=tok)
    assert r.status_code == 201, r.text
    fid = r.json()["id"]

    lst = client.get("/api/v1/search/saved", headers=tok)
    assert lst.status_code == 200
    assert any(x["id"] == fid for x in lst.json())

    # 删除
    d = client.delete(f"/api/v1/search/saved/{fid}", headers=tok)
    assert d.status_code == 200
    lst2 = client.get("/api/v1/search/saved", headers=tok)
    assert all(x["id"] != fid for x in lst2.json())


def test_invalid_sort_falls_back_to_created_at(client):
    cs = _login(client, "customer_service1")
    r = client.get("/api/v1/search/cases",
                   params={"sort_by": "requested_amount); DROP TABLE refund_cases--", "size": 5},
                   headers=_auth(cs))
    assert r.status_code == 200  # 非法排序字段回退 created_at，不报错、不注入


def test_saved_search_delete_other_user_404(client):
    """只能删自己的筛选器：他人的返回 404（业务上不可见）。"""
    cs1 = _login(client, "customer_service1")
    cs2 = _login(client, "supervisor1")
    r = client.post("/api/v1/search/saved", json={"name": "他的", "filters": {}},
                    headers=_auth(cs1))
    fid = r.json()["id"]
    d = client.delete(f"/api/v1/search/saved/{fid}", headers=_auth(cs2))
    assert d.status_code == 404
