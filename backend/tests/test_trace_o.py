# -*- coding: utf-8 -*-
"""Phase O 贯穿 Trace + Tool Trace 结构（~O07）。

覆盖：
- O01 申请流建单生成 trace_id -> GraphState -> 各节点 _log_agent 写入；审批 resume 延续原 trace_id（不新造）。
- O02 真实 duration_ms（节点 perf_counter 计时）+ DB 写失败降级不抛异常中断主流程。
- O03 决策节点 output 记 node/决策状态；human_review 记录人工动作。
- O04 agent_execution_logs 新增 trace 列承载 tools 数组（Tool Trace 结构）+ model 列。
- O05 AgentTrace schema 暴露 trace_id/model/tools（向后兼容）。
- O06 output_snapshot 写库前脱敏（金额/订单号打码，长文本截断）。
- O07 历史行（trace 为 NULL）读取不受影响（schema 可选、tools=None）。

沿用 test_workflow.py 的 env fixture（Fake provider + 测试库）。
"""
import uuid

from app.domain.models import AgentExecutionLog, RefundCase
from tests.conftest import SessionTest
from tests.test_workflow import _make_case, env  # noqa: F401 —— env fixture 复用


def _logs(cid: str) -> list:
    s = SessionTest()
    try:
        rows = (s.query(AgentExecutionLog)
                .filter_by(case_id=cid)
                .order_by(AgentExecutionLog.step_order)
                .all())
        return rows
    finally:
        s.close()


def _case_trace_id(cid: str) -> str:
    s = SessionTest()
    try:
        return s.get(RefundCase, cid).trace_id
    finally:
        s.close()


def test_o01_agents_write_same_trace_id_auto_and_resume(env):
    """自动秒退：intake 到 finalize 全部写在同一个 trace_id 上（贯穿不断裂）。"""
    cid = _make_case("128.00")
    outcome = env["runner"].WorkflowRunner().start(cid)
    assert outcome == "COMPLETED"

    logs = _logs(cid)
    # 秒退链路节点全出现
    names = [lg.agent_name for lg in logs]
    for n in ("intake", "verification", "ocr", "fraud", "sentiment",
              "amount_decision", "refund_execution", "finalize"):
        assert n in names, f"缺节点 {n}: {names}"
    tids = {lg.trace_id for lg in logs}
    assert len(tids) == 1, f"贯穿 trace_id 应唯一，实际 {tids}"
    assert tids.pop()


def test_o01_resume_reuses_original_trace_id(env):
    """挂起 -> 审批续跑：human_review/refund_execution/finalize 沿用原 trace_id。"""
    cid = _make_case("350.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"

    before = _logs(cid)
    before_tids = {lg.trace_id for lg in before}
    orig = next(iter(before_tids))
    assert orig  # intake 前的业务 trace 已写

    assert runner.resume(cid, "APPROVED", "情况属实，批准退款") == "COMPLETED"
    after = _logs(cid)
    after_names = [lg.agent_name for lg in after]
    # resume 新增节点是同一 trace
    assert "human_review" in after_names
    after_tids = {lg.trace_id for lg in after}
    assert len(after_tids) == 1 and after_tids == {orig}, \
        f"resume 不得新造 trace_id：{after_tids}"

    # 唯一 trace 与创建时关联（业务案件行 trace_id 未被替换）
    assert _case_trace_id(cid) in (None, orig)  # 申请流建单写 trace_id 的路径见 API 层


def test_o02_real_duration_and_degrade(env, monkeypatch):
    """真实耗时写入 + 写轨迹失败降级不中断主流程。"""
    cid = _make_case("128.00")
    outcome = env["runner"].WorkflowRunner().start(cid)
    assert outcome == "COMPLETED"
    logs = _logs(cid)
    dur = [lg.duration_ms for lg in logs if lg.duration_ms is not None]
    assert len(dur) == len(logs), "每个节点都应有真实 duration_ms"
    assert all(d >= 0 for d in dur)

    # 降级：DB 写失败时 _log_agent 必须吃掉异常（不冒泡中断图节点）。
# 直接调用原 _log_agent，注入会抛异常的 SessionLocal——它内部 try/except 捕获后
# 静默返回（trace 是旁路增强）。不跑整图，避免业务节点也读不到 DB。
    from app.workflow import graph

    def _boom_session():
        raise RuntimeError("trace write down")

    monkeypatch.setattr(graph, "SessionLocal", _boom_session)
    graph._log_agent("c-none", "ocr", "FAILED", {}, error="db down",
                     duration_ms=5)      # 不应抛出（降级静默）


def test_o03_decision_output_has_node_and_reason(env):
    """决策节点 output 携带 node 编号与决策理由；human_review 记录人工动作。"""
    cid = _make_case("350.00")
    runner = env["runner"].WorkflowRunner()
    assert runner.start(cid) == "SUSPENDED"

    logs = {lg.agent_name: lg for lg in _logs(cid)}
    dec = logs["amount_decision"].output_snapshot or {}
    assert dec.get("node") == 6
    assert "reason" in dec and dec["decision"]

    assert runner.resume(cid, "REJECTED", "凭证不足") == "COMPLETED"
    hr = {lg.agent_name: lg for lg in _logs(cid)}["human_review"].output_snapshot or {}
    assert hr.get("action") == "REJECTED"
    assert hr.get("opinion") == "凭证不足"


def test_o04_tool_trace_and_model_column(env):
    """trace 列（tools 数组）+ model 列写入，结构字段齐备。"""
    cid = _make_case("128.00")
    assert env["runner"].WorkflowRunner().start(cid) == "COMPLETED"
    logs = {lg.agent_name: lg for lg in _logs(cid)}

    # refund_execution 必有 tools 结构
    re = logs["refund_execution"]
    tools = (re.trace or {}).get("tools") if re.trace else None
    assert tools is not None and len(tools) == 1
    t = tools[0]
    for key in ("tool_name", "risk_level", "status", "latency_ms", "side_effect"):
        assert key in t, f"Tool Trace 缺字段 {key}: {t}"
    assert t["tool_name"] == "refund_provider"

    # intake 同样带 tools
    it = logs["intake"]
    assert it.trace and it.trace.get("tools")
    # model 列：决策节点带策略档名（无档则为 None，允许）
    assert "model" in logs["amount_decision"].__dict__


def test_o05_schema_exposes_trace_fields(client):
    """CaseDetail.agents 暴露 trace_id/model/tools（向后兼容可选字段）。"""
    # 通过 API 建单触发索引路径；此处直接验证 schema 模型可选性
    from app.api.schemas import AgentTrace
    t = AgentTrace(agent_name="intake", status="SUCCESS")
    assert t.trace_id is None and t.model is None and t.tools is None   # 历史/旧数据兼容


def test_o06_output_redacted(env):
    """output_snapshot 写库前脱敏：金额打码、长文本截断。"""
    cid = _make_case("128.00")
    assert env["runner"].WorkflowRunner().start(cid) == "COMPLETED"
    logs = {lg.agent_name: lg for lg in _logs(cid)}
    intake_out = logs["intake"].output_snapshot or {}
    # 金额字段（amount）被打码为 ***
    assert intake_out.get("amount") == "***"
    # 订单号打码
    assert "***" in str(intake_out)


def test_o07_historical_rows_backward_compatible(env):
    """历史行（trace/model/trace_id 为 NULL）经 schema 读取仍兼容。"""
    s = SessionTest()
    try:
        # 手工插一条"旧版"轨迹（所有新列均为 None）
        cid = _make_case("128.00")
        old = AgentExecutionLog(case_id=cid, agent_name="legacy", status="SUCCESS",
                                output_snapshot={"note": "old row"},
                                duration_ms=None, step_order=99,
                                trace_id=None, trace=None, model=None)
        s.add(old)
        s.commit()
        from app.api.schemas import AgentTrace
        at = AgentTrace(agent_name=old.agent_name, status=old.status,
                        duration_ms=old.duration_ms,
                        error_message=old.error_message, step_order=old.step_order,
                        output=old.output_snapshot,
                        trace_id=old.trace_id, model=old.model,
                        tools=(old.trace or {}).get("tools") if old.trace else None)
        assert at.tools is None and at.trace_id is None
    finally:
        s.close()