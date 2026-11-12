# -*- coding: utf-8 -*-
"""意图可靠性增强单测（V2.0 -4 / 计划 test_intent_multi.py）。

覆盖：多意图拆分 / 冲突检测 / 规则弱命中→混合来源hybrid / LLM网关(退避+Provider切换+全挂None)
/ 落库 conflict_candidates+route_source / 端点（多拆分/反馈生命周期/意图看板）。
"""
import uuid
from decimal import Decimal

from app.domain.models import (
    CASE_STATUS_RUNNING,
    DECISION_PENDING,
    INTENT_QUERY,
    INTENT_REFUND,
    IntentResult,
    RefundCase,
    User,
)
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login

from app.services import intent_router as ir
from app.services.intent_router import (
    INTENT_HUMAN,
    RULE_CONFIDENCE_MEDIUM,
    analyze_multi_intents,
    build_llm_route,
    record_feedback_sample,
    route_intent,
)


def _make_case() -> str:
    s = SessionTest()
    u = s.query(User).filter_by(username="customer_service1").first()
    if u is None:
        u = User(username="customer_service1", password_hash="x",
                 role="customer_service")
        s.add(u)
        s.flush()
    c = RefundCase(order_id=f"ORD-MI-{uuid.uuid4().hex[:12]}",
                   requested_amount=Decimal("99.00"),
                   submitter_id=u.id,
                   current_status=CASE_STATUS_RUNNING,
                   final_decision=DECISION_PENDING,
                   refund_reason="")
    s.add(c)
    s.flush()
    cid = c.id
    s.commit()
    s.close()
    return cid


# ---------- 多意图拆分 / 冲突检测（纯函数） ----------

def test_multi_intent_split_candidates_sorted_desc():
    """同文本命中多意图：候选独立命中、按置信降序。"""
    an = analyze_multi_intents("我要退款但也要投诉你们，还要转人工")
    intents = [c["intent"] for c in an.candidates]
    assert INTENT_REFUND in intents
    assert "COMPLAINT_ESCALATE" in intents
    assert INTENT_HUMAN in intents             # 3 意图共存
    confs = [c["confidence"] for c in an.candidates]
    assert confs == sorted(confs, reverse=True)  # 置信降序
    assert an.primary == intents[0]


def test_conflict_detection_rule():
    """退款+投诉 -> conflict=True（次高置信达标）；单一意图 -> conflict=False。"""
    an = analyze_multi_intents("我要退款也要投诉你们")
    assert an.conflict is True
    assert len(an.candidates) >= 2

    single = analyze_multi_intents("我要申请退款，钱退到我账户")
    assert single.conflict is False
    assert [c["intent"] for c in single.candidates] == [INTENT_REFUND]


def test_multi_as_dict_contract():
    """as_dict 契约：conflict/primary/candidates 字段，供 /intent/multi 端点输出。"""
    an = analyze_multi_intents("退款还说要投诉")
    d = an.as_dict()
    assert set(d) == {"conflict", "primary", "candidates"}
    assert isinstance(d["candidates"], list) and d["candidates"]


# ---------- 规则弱命中 -> 混合来源 hybrid ----------

def test_hybrid_rule_weak_llm_diverges():
    """弱命中(置信<0.85) + LLM 给出不同意图 -> route_source=hybrid 且 final 仍按规则保守。

    注意：文本须只命中一个规则词（"我要退款"会同时命中"退款"+"要退款"→0.95 不触发）。
    """
    llm = lambda prompt: {"content": '{"intent": "QUERY", "confidence": 0.9}'}
    res = route_intent(None, case_id="x", text="给我退钱",
                       llm_route=llm, persist=False)
    assert res.final_intent == INTENT_REFUND        # 规则优先（保守）
    assert res.route_source == "hybrid"
    assert res.combined_intent == INTENT_QUERY       # LLM 分歧记录
    assert res.level_2_llm == INTENT_QUERY
    assert res.confidence == RULE_CONFIDENCE_MEDIUM  # 不因 LLM 注水


def test_hybrid_rule_weak_llm_agrees_stays_rule():
    """LLM 与规则一致 -> 仍是规则来源（无分歧，不标记 hybrid）。"""
    llm = lambda prompt: {"content": '{"intent": "REFUND", "confidence": 0.9}'}
    res = route_intent(None, case_id="x", text="给我退钱",
                       llm_route=llm, persist=False)
    assert res.route_source == "rule"
    assert res.combined_intent is None
    assert res.level_2_llm is None


def test_strong_rule_hit_no_hybrid():
    """强命中(≥2词,置信0.95) 即使 LLM 分歧也不交叉验证（确定性优先）。"""
    llm = lambda prompt: {"content": '{"intent": "QUERY", "confidence": 0.9}'}
    res = route_intent(None, case_id="x",
                       text="我要申请退款，钱退到我账户",
                       llm_route=llm, persist=False)
    assert res.route_source == "rule"
    assert res.combined_intent is None
    assert res.risk == "LOW"


# ---------- LLM 网关：退避 / Provider 切换 / 全挂 None ----------

class _StubProvider:
    def __init__(self, *, content=None, exc=None, fail_first=0):
        self.content, self.exc = content, exc
        self.fail_first = fail_first
        self.calls: list[str] = []

    def chat_json(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.exc:
            raise self.exc
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("flaky fail")
        return self.content


def _stub_infra(monkeypatch, cloud=None, ollama=None):
    """把 build_llm_route 用的 infra 换桩 + 接管 time.sleep 记录退避。"""
    import types
    import app.infrastructure.llm as infra
    sleeps: list[float] = []

    def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ir, "time", types.SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(infra, "CloudLLMProvider",
                        lambda *a, **k: cloud or _StubProvider(exc=RuntimeError("cloud down")))
    monkeypatch.setattr(infra, "OllamaProvider",
                        lambda *a, **k: ollama or _StubProvider(exc=RuntimeError("ollama down")))
    return sleeps


def test_gateway_cloud_first_without_backoff(monkeypatch):
    """云正常：直接返回 provider=cloud，Ollama 不触发，退避一次都不发生。"""
    sleeps = _stub_infra(monkeypatch, cloud=_StubProvider(content="ok"))
    route = build_llm_route()
    out = route("p")
    assert out == {"content": "ok", "provider": "cloud"}
    assert sleeps == []


def test_gateway_provider_switch_to_ollama_with_backoff(monkeypatch):
    """云挂 -> Ollama 首轮失败重试，指数退避序列 0.5s -> 1.0s 可见。"""
    sleeps = _stub_infra(
        monkeypatch,
        cloud=_StubProvider(exc=RuntimeError("cloud down")),
        ollama=_StubProvider(content='{"intent": "REFUND"}', fail_first=1),
    )
    route = build_llm_route(max_attempts=3)
    out = route("p")
    assert out["provider"] == "ollama"
    assert out["content"] == '{"intent": "REFUND"}'
    assert sleeps == [0.5, 1.0]        # step=1 -> 0.5s 失败；step=2 -> 1.0s 成功


def test_gateway_all_down_returns_none(monkeypatch):
    """两 Provider 全挂 -> None（上层 Fail-Safe HUMAN），绝不抛错。"""
    _stub_infra(monkeypatch)            # 默认两侧都 raise
    route = build_llm_route()
    assert route("p") is None


def test_gateway_within_route_intent_llm_source(monkeypatch):
    """网关接入 route_intent：规则未命中 -> LLM 层定 REFUND，来源=llm。"""
    _stub_infra(monkeypatch,
                cloud=_StubProvider(content='{"intent": "REFUND", "confidence": 0.8}'))
    route = build_llm_route()
    res = route_intent(None, case_id="x", text="东西坏了很生气",
                       llm_route=route, persist=False)
    assert res.final_intent == INTENT_REFUND
    assert res.route_source == "llm"
    assert res.retries_used == 1


# ---------- 落库：conflict_candidates + route_source ----------

def test_persist_writes_conflict_candidates_and_source():
    """多意图文本落库：conflict_candidates.conflict=True + route_source 记案。"""
    cid = _make_case()
    s = SessionTest()
    route_intent(s, case_id=cid, text="我要退款也要投诉你们",
                 llm_route=None, persist=True)
    row = s.query(IntentResult).filter_by(case_id=cid).first()
    s.close()
    assert row is not None
    assert row.route_source == "rule"
    assert row.conflict_candidates is not None
    assert row.conflict_candidates.get("conflict") is True
    assert row.conflict_candidates.get("primary") == INTENT_REFUND
    assert len(row.conflict_candidates.get("candidates", [])) >= 2


# ---------- 反馈采样入评测候选集 ----------

def test_feedback_record_list_review_adopt_export():
    """生命周期：PENDING -> REVIEWED -> ADOPTED，ADOPTED 进候选导出集。"""
    s = SessionTest()
    row = record_feedback_sample(
        s, case_id=None, route_final_intent=INTENT_REFUND,
        human_corrected_intent=INTENT_QUERY, reason="误路由：应查物流")
    assert row.status == "PENDING"
    items = ir.list_feedback_samples(s, status="PENDING")
    assert any(i["id"] == row.id for i in items)

    reviewed = ir.review_feedback_sample(s, row.id, decision="REVIEWED",
                                         reviewed_by="supervisor1")
    assert reviewed["status"] == "REVIEWED"
    adopted = ir.review_feedback_sample(s, row.id, decision="ADOPTED",
                                        reviewed_by="supervisor1")
    assert adopted["status"] == "ADOPTED"
    assert adopted["reviewed_by"] == "supervisor1"

    cand = ir.export_feedback_candidates(s)
    assert any(c["id"] == row.id and c["human_corrected_intent"] == INTENT_QUERY
               for c in cand)
    s.close()


def test_feedback_state_machine_illegal_jump():
    """PENDING 直接 ADOPTED 非法（必须先 REVIEWED）。"""
    s = SessionTest()
    row = record_feedback_sample(s, case_id=None,
                                 route_final_intent=INTENT_REFUND,
                                 human_corrected_intent=INTENT_QUERY)
    try:
        ir.review_feedback_sample(s, row.id, decision="ADOPTED",
                                  reviewed_by="admin1")
        assert False, "非法跳级应抛 ValueError"
    except ValueError:
        pass
    s.close()


def test_feedback_rejects_illegal_intent():
    """非法意图值拒绝入候选集（防脏数据）。"""
    s = SessionTest()
    try:
        record_feedback_sample(s, case_id=None,
                               route_final_intent="NOT_A_REAL_INTENT",
                               human_corrected_intent=INTENT_QUERY)
        assert False, "非法意图应抛 ValueError"
    except ValueError:
        pass
    s.close()


# ---------- 端点 ----------

def test_intent_multi_endpoint(client):
    """POST /api/v1/intent/multi：多意图/冲突检测输出。"""
    tok = login(client, "customer_service1")
    resp = client.post("/api/v1/intent/multi",
                       headers=_auth_headers(tok),
                       json={"text": "我要退款也要投诉你们"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["conflict"] is True
    assert body["primary"] == INTENT_REFUND


def test_feedback_endpoint_lifecycle(client):
    """POST feedback-sample(客服可) -> PUT review(主管) 完整流转。"""
    cs_tok = login(client, "customer_service1")
    resp = client.post("/api/v1/intent/feedback-sample",
                       headers=_auth_headers(cs_tok),
                       json={"route_final_intent": "REFUND",
                             "human_corrected_intent": "QUERY",
                             "reason": "误路由应查物流"})
    assert resp.status_code == 200
    sample_id = resp.json()["id"]

    # 客服无审核权限
    r403 = client.put(f"/api/v1/intent/feedback/{sample_id}/review",
                      headers=_auth_headers(cs_tok),
                      json={"decision": "REVIEWED"})
    assert r403.status_code == 403

    sup_tok = login(client, "supervisor1")
    r = client.put(f"/api/v1/intent/feedback/{sample_id}/review",
                   headers=_auth_headers(sup_tok),
                   json={"decision": "REVIEWED"})
    assert r.status_code == 200 and r.json()["status"] == "REVIEWED"
    r = client.put(f"/api/v1/intent/feedback/{sample_id}/review",
                   headers=_auth_headers(sup_tok),
                   json={"decision": "ADOPTED"})
    assert r.status_code == 200 and r.json()["status"] == "ADOPTED"

    ex = client.get("/api/v1/intent/feedback/export",
                    headers=_auth_headers(sup_tok))
    assert ex.status_code == 200
    assert any(c["id"] == sample_id for c in ex.json()["candidates"])


def test_intent_metrics_endpoint(client):
    """GET /api/v1/intent/metrics：主管可读，聚合字段齐全。"""
    cid = _make_case()
    s = SessionTest()
    route_intent(s, case_id=cid, text="我要退款也要投诉你们",
                 llm_route=None, persist=True)
    s.close()
    tok = login(client, "supervisor1")
    resp = client.get("/api/v1/intent/metrics?days=7",
                      headers=_auth_headers(tok))
    assert resp.status_code == 200
    body = resp.json()
    # 共享测试库已有其它用例的 intent_results；断言相对/存在性而不是精确值
    assert body["total"] >= 1
    assert 0.0 <= body["rule_hit_rate"] <= 1.0
    assert body["llm_call_rate"] is not None
    assert "tokens" in body              # Token 汇总字段存在
    assert body["conflict_cases"] >= 1   # 本次落库的冲突候选已被计数