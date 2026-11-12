# -*- coding: utf-8 -*-
"""Phase 3 验证：认证、角色权限、工单接口、幂等头、状态机 409、错误格式。

覆盖（docs/08 第3阶段验证清单）：未登录 401 / 客服越权 403 / 过期 JWT 401 /
参数错误 400 / 案件不存在 404 / 非法状态审批 409 / 幂等重复返回首次结果。
"""
import io
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt

from app.core.config import settings


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login(client, username: str, password: str = "123456") -> str:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def new_idem() -> str:
    return f"it-{uuid.uuid4().hex}"


def create_case(client, token: str, amount="128.00", idem=None, order_id=None) -> dict:
    resp = client.post(
        "/api/v1/refunds",
        data={"order_id": order_id or f"ORD-{uuid.uuid4().hex[:8]}", "requested_amount": amount},
        files={"evidence": ("bad.jpg", io.BytesIO(b"fake-image-bytes"), "image/jpeg")},
        headers={**_auth_headers(token), "X-Idempotency-Key": idem or new_idem()},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


# ---- 认证 ----
def test_health(client):
    assert client.get("/api/v1/health").status_code == 200


def test_login_ok(client):
    token = login(client, "supervisor1")
    assert token


def test_login_wrong_password(client):
    resp = client.post("/api/v1/auth/login", json={"username": "supervisor1", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


def test_unauthorized_access(client):
    assert client.get("/api/v1/refunds").status_code == 401


def test_expired_jwt(client):
    token = jwt.encode(
        {"sub": "x", "role": "supervisor", "exp": datetime.now(timezone.utc) - timedelta(minutes=5)},
        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
    )
    assert client.get("/api/v1/refunds", headers=_auth_headers(token)).status_code == 401


# ---- 工单创建 ----
def test_create_refund_202(client):
    token = login(client, "customer_service1")
    body = create_case(client, token)
    assert body["status"] == "RUNNING"
    assert body["case_id"]


def test_create_missing_idempotency_key(client):
    token = login(client, "customer_service1")
    resp = client.post(
        "/api/v1/refunds",
        data={"order_id": "ORD-X", "requested_amount": "128.00"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 400


def test_create_invalid_amount(client):
    token = login(client, "customer_service1")
    resp = client.post(
        "/api/v1/refunds",
        data={"order_id": "ORD-X", "requested_amount": "0"},
        headers={**_auth_headers(token), "X-Idempotency-Key": new_idem()},
    )
    assert resp.status_code == 400


def test_create_duplicate_idempotency_returns_first(client):
    token = login(client, "customer_service1")
    idem = new_idem()
    first = create_case(client, token, idem=idem)
    second = create_case(client, token, idem=idem)
    assert second["case_id"] == first["case_id"]


def test_invalid_file_type(client):
    token = login(client, "customer_service1")
    resp = client.post(
        "/api/v1/refunds",
        data={"order_id": "ORD-X", "requested_amount": "128.00"},
        files={"evidence": ("doc.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
        headers={**_auth_headers(token), "X-Idempotency-Key": new_idem()},
    )
    assert resp.status_code == 400


# ---- 详情 / 列表 ----
def test_case_detail_404(client):
    token = login(client, "supervisor1")
    assert client.get("/api/v1/refunds/nonexistent", headers=_auth_headers(token)).status_code == 404


def test_list_and_detail(client):
    token = login(client, "customer_service1")
    body = create_case(client, token)
    detail = client.get(f"/api/v1/refunds/{body['case_id']}", headers=_auth_headers(token))
    assert detail.status_code == 200
    j = detail.json()
    assert j["current_status"] == "RUNNING"
    # ：详情必须展示三态/决策/退款状态/核验结果（M4）
    assert j["refund_status"] == "NONE"
    assert "order_verification" in j
    assert "product_verification" in j
    assert "price_verification" in j
    assert j["decision_scores"]["version"] == "decision-score-v1"
    assert [item["key"] for item in j["decision_scores"]["dimensions"]] == [
        "evidence_confidence", "fraud_risk", "sentiment_risk",
    ]
    # 展示强化：核验理由 + 退款回执 + Trace（step_order/output）
    assert "order_verify_reasons" in j and j["order_verify_reasons"] == []
    assert "refund_transactions" in j and j["refund_transactions"] == []
    assert "agents" in j
    if j["agents"]:  # 新建案件可能有 intake 轨迹
        first = j["agents"][0]
        assert "step_order" in first and "output" in first
    assert "eval_run_id" in j and j["eval_run_id"] is None  # 非评测案件
    listing = client.get("/api/v1/refunds", headers=_auth_headers(token))
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1


def test_supervisor_can_view_original_evidence(client):
    """订单凭证原图只能经鉴权接口读取，供主管对照 OCR 结果审批。"""
    cs_token = login(client, "customer_service1")
    sup_token = login(client, "supervisor1")
    body = create_case(client, cs_token)
    url = f"/api/v1/refunds/{body['case_id']}/evidence"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=_auth_headers(cs_token)).status_code == 403

    response = client.get(url, headers=_auth_headers(sup_token))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content == b"fake-image-bytes"


def test_create_refund_accepts_multiple_evidences_and_lists_them(client):
    cs_token = login(client, "customer_service1")
    sup_token = login(client, "supervisor1")
    response = client.post(
        "/api/v1/refunds",
        data={"order_id": f"ORD-{uuid.uuid4().hex[:8]}", "requested_amount": "128.00",
              "refund_reason": "商品屏幕破裂，申请退货退款", "reason_codes": "退货退款"},
        files=[
            ("evidence", ("order.png", io.BytesIO(b"order-image"), "image/png")),
            ("evidences", ("damage-front.jpg", io.BytesIO(b"damage-front"), "image/jpeg")),
            ("evidences", ("damage-side.jpg", io.BytesIO(b"damage-side"), "image/jpeg")),
        ],
        headers={**_auth_headers(cs_token), "X-Idempotency-Key": new_idem()},
    )
    assert response.status_code == 202, response.text
    case_id = response.json()["case_id"]

    listing = client.get(
        f"/api/v1/refunds/{case_id}/evidences", headers=_auth_headers(sup_token)
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert len(items) == 3
    assert {item["file_name"] for item in items} == {
        "order.png", "damage-front.jpg", "damage-side.jpg",
    }

    image = client.get(
        f"/api/v1/refunds/{case_id}/evidences/{items[1]['evidence_id']}",
        headers=_auth_headers(sup_token),
    )
    assert image.status_code == 200


def test_eval_capabilities_and_detail_eval_metadata(client):
    """M4：/v1/evaluation/capabilities 协议 + 评测头写入案件 eval_run_id。"""
    k = client.get("/api/v1/evaluation/capabilities")
    assert k.status_code == 200
    caps = k.json()
    assert caps["protocol_version"] == "1.0"
    assert caps["target_type"] == "refund_workflow"
    assert caps["capabilities"]["hitl"] is True
    assert set(caps["extensions"]) == {"security", "intent", "voice", "coaching"}

    # 评测头写入案件（M8 隔离基础）
    token = login(client, "customer_service1")
    r = client.post(
        "/api/v1/refunds",
        data={"order_id": f"ORD-EVAL-{uuid.uuid4().hex[:8]}", "requested_amount": "99.00",
              "refund_reason": "评测"},
        headers={
            **_auth_headers(token),
            "X-Idempotency-Key": f"idem-eval-{uuid.uuid4().hex[:8]}",
            "X-Eval-Run-Id": "run-1",
            "X-Eval-Case-Id": "G01",
        },
    )
    assert r.status_code == 202
    cid = r.json()["case_id"]
    detail = client.get(f"/api/v1/refunds/{cid}", headers=_auth_headers(token)).json()
    assert detail["eval_run_id"] == "run-1"
    assert detail["eval_case_id"] == "G01"


# ---- 审批权限与状态机 ----
def test_customer_service_cannot_approve(client):
    cs_token = login(client, "customer_service1")
    sup_token = login(client, "supervisor1")
    case = create_case(client, cs_token)
    resp = client.post(
        f"/api/v1/refunds/{case['case_id']}/approve",
        data={"opinion": "越权尝试"},
        headers={**_auth_headers(cs_token), "X-Idempotency-Key": new_idem()},
    )
    assert resp.status_code == 403


def test_approve_running_case_409(client):
    """RUNNING 案件不能审批（状态机要求 SUSPENDED 才能进入人工终态）。"""
    cs_token = login(client, "customer_service1")
    sup_token = login(client, "supervisor1")
    case = create_case(client, cs_token)
    resp = client.post(
        f"/api/v1/refunds/{case['case_id']}/approve",
        data={"opinion": "还没挂起"},
        headers={**_auth_headers(sup_token), "X-Idempotency-Key": new_idem()},
    )
    assert resp.status_code == 409


def test_approve_suspended_case_success_and_no_repeat(client):
    cs_token = login(client, "customer_service1")
    sup_token = login(client, "supervisor1")
    case = create_case(client, cs_token)
    cid = case["case_id"]

    # 手动把工单置为 SUSPENDED（挂起动作在 Phase 5 由 LangGraph interrupt 产生；这里直接落库模拟）
    from app.domain.models import CASE_STATUS_SUSPENDED, RefundCase
    from tests.conftest import SessionTest
    s = SessionTest()
    row = s.get(RefundCase, cid)
    row.current_status = CASE_STATUS_SUSPENDED
    s.commit()
    s.close()

    idem = new_idem()
    resp = client.post(
        f"/api/v1/refunds/{cid}/approve",
        data={"opinion": "情况属实，批准退款"},
        headers={**_auth_headers(sup_token), "X-Idempotency-Key": idem},
    )
    assert resp.status_code == 202
    # 唤醒图模型：案件保持 SUSPENDED，由 Worker 恢复图后推进终态
    assert resp.json()["current_status"] == "SUSPENDED"
    assert resp.json()["action"] == "APPROVED"

    # 重复审批（新幂等键）：已有审批记录 -> 409
    resp2 = client.post(
        f"/api/v1/refunds/{cid}/approve",
        data={"opinion": "重复审批"},
        headers={**_auth_headers(sup_token), "X-Idempotency-Key": new_idem()},
    )
    assert resp2.status_code == 409


def test_reject_suspended_case(client):
    cs_token = login(client, "customer_service1")
    sup_token = login(client, "supervisor1")
    case = create_case(client, cs_token)
    cid = case["case_id"]

    from app.domain.models import CASE_STATUS_SUSPENDED, RefundCase
    from tests.conftest import SessionTest
    s = SessionTest()
    row = s.get(RefundCase, cid)
    row.current_status = CASE_STATUS_SUSPENDED
    s.commit()
    s.close()

    resp = client.post(
        f"/api/v1/refunds/{cid}/reject",
        data={"opinion": "凭证不足，拒绝"},
        headers={**_auth_headers(sup_token), "X-Idempotency-Key": new_idem()},
    )
    assert resp.status_code == 202
    assert resp.json()["action"] == "REJECTED"


def test_dashboard_stats(client):
    token = login(client, "supervisor1")
    resp = client.get("/api/v1/dashboard/stats", headers=_auth_headers(token))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"total", "running", "suspended", "completed"}
