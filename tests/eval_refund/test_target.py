# -*- coding: utf-8 -*-
"""M4 RefundSystemHttpTarget 黑盒 HTTP 契约测试。

用 httpx.MockTransport 模拟被测系统（SUT）的 `/api/v1/*` 响应，验证 Target 对
登录/建单/轮询/识别挂起/审批/拒绝/读终态的各类分支：成功路径、目标不可用、
协议错误（非 JSON / 缺字段）都必须明确失败，绝不伪造 pass。
"""
import httpx
import pytest

from eval_refund.schema import (
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    STATUS_COMPLETED,
    STATUS_SUSPENDED,
)
from eval_refund.target import RefundSystemHttpTarget, TargetError


def _auth_ready(t: RefundSystemHttpTarget) -> None:
    """预设 token，避免每个用例重复真实/伪造 login。"""
    t._token = "tok123"


class _FakeSUT:
    """最小被测系统模拟：内存案件状态 + 登录，支持 approve/reject 后进终态。"""

    def __init__(self) -> None:
        self.cases: dict[str, dict] = {}
        self.login_calls = 0
        self.return_non_json = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login":
            self.login_calls += 1
            if self.return_non_json:
                return httpx.Response(200, text="<html>oops</html>")
            return httpx.Response(200, json={"token": "tok123"})
        if path == "/api/v1/refunds" and request.method == "POST":
            # 返回响应体由测试通过本类预设 case 决定：这里默认建单成功
            return httpx.Response(202, json={"case_id": "case-new"})
        if path.startswith("/api/v1/refunds/") and request.method == "GET":
            cid = path.rsplit("/", 1)[-1]
            c = self.cases.get(cid)
            if c is None:
                return httpx.Response(404, json={"detail": "案件不存在"})
            return httpx.Response(200, json=dict(c))
        if path.endswith("/approve") or path.endswith("/reject"):
            cid = path.split("/")[-2]
            action = "approve" if path.endswith("/approve") else "reject"
            c = self.cases.get(cid)
            if c is None:
                return httpx.Response(404, json={"detail": "案件不存在"})
            self._advance(c, action)
            return httpx.Response(202, json={"case_id": cid, "status": action})
        return httpx.Response(404, json={"detail": "not found"})

    def _advance(self, c: dict, action: str) -> None:
        c["current_status"] = STATUS_COMPLETED
        c["final_decision"] = DECISION_APPROVED if action == "approve" else DECISION_REJECTED
        c["refund_status"] = "SUCCESS" if action == "approve" else "NONE"
        c["_polling_rounds"] = 1


@pytest.fixture()
def target() -> tuple[RefundSystemHttpTarget, _FakeSUT]:
    sut = _FakeSUT()
    transport = httpx.MockTransport(sut.handle)
    client = httpx.Client(transport=transport, base_url="http://fake")
    t = RefundSystemHttpTarget(base_url="http://fake",
                               poll_interval=0.001, poll_max_seconds=0.2)
    t._client = client
    return t, sut


def test_login_success(target):
    t, sut = target
    assert t.login("customer_service1", "123456") == "tok123"
    assert sut.login_calls == 1
    assert t.auth_headers["Authorization"] == "Bearer tok123"


def test_login_non_json_response_raises(target):
    t, sut = target
    sut.return_non_json = True
    with pytest.raises(TargetError, match="token"):
        t.login("u", "p")


def test_login_unavailable_raises(target):
    """连接级错误（目标不可用）-> TargetError，不伪造 pass。"""
    t, sut = target
    sock = httpx.Client(transport=httpx.MockTransport(
        handler=lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused"))),
        base_url="http://fake")
    t._client = sock
    with pytest.raises(TargetError, match="login"):
        t.login("u", "p")


def test_create_case_non_202_raises(target):
    t, sut = target
    _auth_ready(t)
    sock = httpx.Client(transport=httpx.MockTransport(
        handler=lambda req, **kw: httpx.Response(400, json={"detail": "bad"})),
        base_url="http://fake")
    t._client = sock
    with pytest.raises(TargetError, match="create_case"):
        t.create_case(order_id="ORD-X", requested_amount="99.00")


def test_create_case_missing_case_id_raises(target):
    t, sut = target
    _auth_ready(t)
    sock = httpx.Client(transport=httpx.MockTransport(
        handler=lambda req: httpx.Response(202, json={})), base_url="http://fake")
    t._client = sock
    with pytest.raises(TargetError, match="case_id"):
        t.create_case(order_id="ORD-X", requested_amount="99.00")


def test_wait_terminal_approve_completed(target):
    """SUSPENDED -> approve -> COMPLETED/APPROVED 返回终态。"""
    t, sut = target
    _auth_ready(t)
    cid = "case-approve"
    sut.cases[cid] = {
        "case_id": cid, "current_status": STATUS_SUSPENDED,
        "final_decision": DECISION_PENDING, "refund_status": "NONE",
    }
    sut._advance(sut.cases[cid], "approve")  # 模拟主管已审批
    res = t.wait_terminal(cid, decision=DECISION_APPROVED)
    assert res["current_status"] == STATUS_COMPLETED
    assert res["final_decision"] == DECISION_APPROVED
    assert res["_polling_rounds"] == 1


def test_wait_terminal_timeout_raises(target):
    """轮询超时（案件永久挂起/卡住）-> TargetError（不是 pass）。"""
    t, sut = target
    _auth_ready(t)
    cid = "case-timeout"
    sut.cases[cid] = {
        "case_id": cid, "current_status": STATUS_SUSPENDED,
        "final_decision": DECISION_PENDING, "refund_status": "NONE",
        "current_graph_node": "human_review",
    }
    with pytest.raises(TargetError):
        t.wait_terminal(cid)


def test_wait_terminal_wrong_final_decision_raises(target):
    """到终态但决策与预期不符 -> 明确报错。"""
    t, sut = target
    _auth_ready(t)
    cid = "case-wrong"
    sut.cases[cid] = {
        "case_id": cid, "current_status": STATUS_COMPLETED,
        "final_decision": DECISION_REJECTED, "refund_status": "NONE",
    }
    with pytest.raises(TargetError, match="决策不符"):
        t.wait_terminal(cid, decision=DECISION_APPROVED)


def test_get_case_not_found_raises(target):
    t, sut = target
    with pytest.raises(TargetError):
        t.get_case("nonexistent")


def test_decide_bad_action_rejected_locally(target):
    """非法 action 在本地即拒绝，不发起网络请求。"""
    t, sut = target
    _auth_ready(t)
    with pytest.raises(TargetError):
        t.decide("c", "pwn", "x")


def test_decide_approve_completes_case(target):
    t, sut = target
    _auth_ready(t)
    cid = "case-decide"
    sut.cases[cid] = {
        "case_id": cid, "current_status": STATUS_SUSPENDED,
        "final_decision": DECISION_PENDING, "refund_status": "NONE",
    }
    res = t.decide(cid, "approve", "评测人工审批")
    assert res["status"] == "approve"
    assert sut.cases[cid]["final_decision"] == DECISION_APPROVED


def test_run_case_auto_completed_to_candidate(target):
    """案件自动到 COMPLETED/APPROVED（无挂起）-> 规范化为 CandidateOutput。"""
    t, sut = target
    cid = "G-auto"
    # create_case 返回 case-new，预置该终态
    sut.cases["case-new"] = {
        "case_id": "case-new", "current_status": STATUS_COMPLETED,
        "final_decision": DECISION_APPROVED, "refund_status": "SUCCESS",
        "current_graph_node": "finalize",
    }
    golden = {
        "case_id": cid,
        "body": {"order_id": "ORD-x", "requested_amount": "99.00",
                 "refund_reason": "破损"},
        "expected": {"final_decision": DECISION_APPROVED},
    }
    out = t.run_case(golden)
    assert out.case_id == "case-new"
    assert out.current_status == STATUS_COMPLETED
    assert out.final_decision == DECISION_APPROVED
    assert out.refund_status == "SUCCESS"
    assert out.target_type == "refund_workflow"
    assert out.schema_version == "1.0"


def test_run_case_requires_expected_decision_to_wake(target):
    """SUSPENDED 且 golden 未声明 expected 决策 -> 不自动伪造审批，报告挂起。"""
    t, sut = target
    cid = "G-suspend"
    sut.cases["case-new"] = {
        "case_id": "case-new", "current_status": STATUS_SUSPENDED,
        "final_decision": DECISION_PENDING, "refund_status": "NONE",
        "current_graph_node": "human_review",
    }
    golden = {
        "case_id": cid,
        "body": {"order_id": "ORD-x", "requested_amount": "99.00"},
        "expected": {"final_decision": DECISION_PENDING},
    }
    out = t.run_case(golden)
    # 不伪造审批：案件保持挂起报告（也是合法终态的一种）
    assert out.current_status == STATUS_SUSPENDED
    assert out.final_decision == DECISION_PENDING