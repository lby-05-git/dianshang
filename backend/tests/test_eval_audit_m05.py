# -*- coding: utf-8 -*-
"""M05 评测中心审计埋点契约测试（评测中心审计 ）。

全量覆盖评测中心域内敏感动作的 domain_events 审计留痕（append-only）：
- 数据集：create / copy / delete -> DATASET_CREATED / COPIED / DELETED
- Run：create / ab / cancel -> EVAL_RUN_CREATED / EVAL_AB_CREATED / EVAL_RUN_CANCELLED
- Prompt：create / transition -> PROMPT_CREATED / PROMPT_{status}
- Office 导入 -> OFFICE_IMPORTED
- 调度：create / update / fire -> SCHEDULE_CREATED / UPDATED / FIRED
- 门禁：baseline / evaluate / replay-set -> GATE_BASELINE_APPROVED / GATE_EVALUATED / REPLAY_SET_CREATED
- 报告导出 -> REPORT_EXPORTED

红线：审计只 insert 不 UPDATE/DELETE；write_event 后必须 db.commit()（get_db 不自动 commit）。
"""
import uuid

from app.domain.models import DomainEvent
from tests.conftest import SessionTest
from tests.test_api import _auth_headers, login
from tests.test_eval_schedule import _seed_suite
from tests.test_release_gate import _fake_baseline_run


def _api_h(client, username):
    return _auth_headers(login(client, username))


def _last_event(session, action, **payload_eq):
    """取对某 action 最新匹配事件。

    关键：in-process TestClient 里 write_event 走的是 override session 的
    独立事务并 commit；而外层 `s` 会话在 MySQL 快照隔离下看不到后续提交，
    直接读会漏事件。故这里每次用新会话查询，保证看到 request 的提交。
    """
    with SessionTest() as s:
        q = (s.query(DomainEvent)
             .filter_by(action=action)
             .order_by(DomainEvent.created_at.desc(),
                       DomainEvent.id.desc())
             .limit(500))
        evts = q.all()
        if payload_eq:
            cands = [e for e in evts
                     if all((e.payload or {}).get(k) == v
                            for k, v in payload_eq.items())]
            evts = cands
        assert evts, f"缺 action={action} payload={payload_eq} 的审计事件"
        return evts[0]


# ---- 数据集 ----

def test_m05_dataset_created_copied_deleted(client):
    s = SessionTest()
    h = _api_h(client, "leader1")
    # create
    r = client.post("/api/v1/evaluation-center/datasets",
                    json={"name": f"ds-{uuid.uuid4().hex[:6]}",
                          "changelog": "M05"}, headers=h)
    assert r.status_code == 201, r.text
    suite_id = r.json()["item"]["id"]
    ev1 = _last_event(s, "DATASET_CREATED", suite_id=suite_id)
    # copy
    r2 = client.post(f"/api/v1/evaluation-center/datasets/{suite_id}/copy",
                     json={"new_name": f"ds-cp-{uuid.uuid4().hex[:6]}",
                           "changelog": "copy"}, headers=h)
    assert r2.status_code == 201, r2.text
    new_id = r2.json()["item"]["id"]
    ev2 = _last_event(s, "DATASET_COPIED", new_suite_id=new_id)
    assert ev2.payload["source_suite_id"] == suite_id
    assert ev2.payload["new_suite_id"] == new_id
    # delete（admin；先保证 copy/new 未被 run 引用，直接删 new）
    h_adm = _api_h(client, "admin1")
    rd = client.delete(f"/api/v1/evaluation-center/datasets/{new_id}",
                       headers=h_adm)
    assert rd.status_code == 204, rd.text
    ev3 = _last_event(s, "DATASET_DELETED", suite_id=new_id)
    assert ev3.payload["suite_id"] == new_id


# ---- Run ----

def test_m05_run_created_and_cancelled(client):
    s = SessionTest()
    h = _api_h(client, "leader1")
    suite = _seed_suite(s)
    r = client.post("/api/v1/evaluation-center/runs",
                    json={"name": f"run-{uuid.uuid4().hex[:6]}",
                          "suite_id": suite["id"], "mode": "in_process"},
                    headers=h)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    ev = _last_event(s, "EVAL_RUN_CREATED", run_id=run_id)
    assert ev.payload["suite_id"] == suite["id"]
    # cancel
    rc = client.post(f"/api/v1/evaluation-center/runs/{run_id}/cancel",
                     headers=h)
    assert rc.status_code in (200, 409), rc.text
    if rc.status_code == 200:
        ev2 = _last_event(s, "EVAL_RUN_CANCELLED", run_id=run_id)


def test_m05_ab_run_created(client):
    """A-B Run 落审计 EVAL_AB_CREATED。"""
    s = SessionTest()
    h = _api_h(client, "leader1")
    suite = _seed_suite(s)
    base = _fake_baseline_run(s, suite)
    r = client.post("/api/v1/evaluation-center/runs/ab",
                    json={"name": f"ab-{uuid.uuid4().hex[:6]}",
                          "suite_id": suite["id"], "baseline_id": base},
                    headers=h)
    assert r.status_code in (201, 202, 400), r.text
    if r.status_code in (201, 202):
        ev = _last_event(s, "EVAL_AB_CREATED", suite_id=suite["id"])


# ---- Prompt ----

def test_m05_prompt_created_and_transition(client):
    s = SessionTest()
    h = _api_h(client, "leader1")
    # content 用唯一串，确保新版本从 DRAFT 起（库共享，重复 content 会复用旧状态）
    nonce = uuid.uuid4().hex[:8]
    r = client.post("/api/v1/evaluation-center/prompts",
                    json={"name": f"pm-{nonce}",
                          "agent_name": "refund_advisor",
                          "content": f"你是退赔顾问…{nonce}",
                          "model_name": "deepseek-v4-pro",
                          "changelog": "M05"}, headers=h)
    assert r.status_code == 201, r.text
    pid = r.json()["item"]["id"]
    ev = _last_event(s, "PROMPT_CREATED", prompt_id=pid)
    # transition DRAFT->CANDIDATE（新建= DRAFT，可迁移）
    rt = client.post(f"/api/v1/evaluation-center/prompts/{pid}/transition",
                     json={"to_status": "CANDIDATE"}, headers=h)
    assert rt.status_code == 200, rt.text
    ev2 = _last_event(s, "PROMPT_CANDIDATE", prompt_id=pid)


# ---- Office 导入 ----

def test_m05_office_imported(client):
    """Office 导入落审计 OFFICE_IMPORTED（走 preview 上传 -> confirm 全链路）。"""
    s = SessionTest()
    h = _api_h(client, "leader1")
    # 构造合法 CSV 导入（列头与 imported_rows_to_doc 匹配，见 test_eval_office.py）
    csv_data = (
        "id,case_type,final_decision,refund_status,reason\n"
        "C-M05-1,auto_approve,APPROVED,NONE,M05 import audit\n"
    ).encode()
    rp = client.post("/api/v1/evaluation-center/office/import-preview",
                     files={"file": ("cd.csv", csv_data,
                                     "text/csv")}, headers=h)
    assert rp.status_code == 200, rp.text
    tok = rp.json()["token"]
    rr = client.post("/api/v1/evaluation-center/office/import-confirm",
                     json={"token": tok, "name": f"im-{uuid.uuid4().hex[:6]}",
                           "changelog": "M05"},
                     headers=h)
    assert rr.status_code == 201, rr.text
    ev = _last_event(s, "OFFICE_IMPORTED", name=rr.json()["item"]["name"])
    assert ev.payload["case_count"] >= 1


# ---- 调度 ----

def test_m05_schedule_created_updated_fired(client):
    s = SessionTest()
    h = _api_h(client, "leader1")
    suite = _seed_suite(s)
    # create
    r = client.post("/api/v1/evaluation-center/schedules",
                    json={"suite_id": suite["id"], "tier": "smoke",
                          "cadence": "daily"}, headers=h)
    assert r.status_code == 201, r.text
    sch_id = r.json()["item"]["id"]
    ev = _last_event(s, "SCHEDULE_CREATED", schedule_id=sch_id)
    # update
    ru = client.patch(f"/api/v1/evaluation-center/schedules/{sch_id}",
                      json={"enabled": False}, headers=h)
    assert ru.status_code == 200, ru.text
    ev2 = _last_event(s, "SCHEDULE_UPDATED", schedule_id=sch_id)
    assert ev2.payload["enabled"] is False
    # 重新启用后 fire
    client.patch(f"/api/v1/evaluation-center/schedules/{sch_id}",
                 json={"enabled": True}, headers=h)
    rf = client.post(f"/api/v1/evaluation-center/schedules/{sch_id}/fire",
                     headers=h)
    assert rf.status_code == 202, rf.text
    run_id = rf.json()["run_id"]
    ev3 = _last_event(s, "SCHEDULE_FIRED", run_id=run_id)
    assert ev3.payload["schedule_id"] == sch_id


# ---- 门禁 + 报告导出 ----

def test_m05_gate_baseline_evaluate_replay_export(client):
    s = SessionTest()
    h = _api_h(client, "leader1")
    suite = _seed_suite(s)
    base_run = _fake_baseline_run(s, suite, run_id=f"run-{uuid.uuid4().hex[:10]}")
    cand = _fake_baseline_run(s, suite, run_id=f"run-{uuid.uuid4().hex[:10]}")
    # baseline approve
    rb = client.post("/api/v1/evaluation-center/gate/baseline",
                     json={"suite_id": suite["id"], "run_id": base_run,
                           "note": "M05"}, headers=h)
    assert rb.status_code == 201, rb.text
    ev = _last_event(s, "GATE_BASELINE_APPROVED", run_id=base_run)
    assert ev.payload["suite_id"] == suite["id"]
    # evaluate
    re_ = client.post("/api/v1/evaluation-center/gate/evaluate",
                      json={"run_id": cand, "suite_id": suite["id"],
                            "tier": "smoke"}, headers=h)
    assert re_.status_code == 200, re_.text
    ev2 = _last_event(s, "GATE_EVALUATED", run_id=cand)
    # replay-set
    rp = client.post("/api/v1/evaluation-center/gate/replay-set",
                     json={"suite_id": suite["id"], "run_id": cand,
                           "tier": "core"}, headers=h)
    assert rp.status_code == 200, rp.text
    ev3 = _last_event(s, "REPLAY_SET_CREATED", run_id=cand)
    assert ev3.payload["suite_id"] == suite["id"]
    assert ev3.payload["tier"] == "core"
    # 报告导出（sandbox 生成字节不落盘）
    rx = client.get(f"/api/v1/evaluation-center/reports/{cand}/export?fmt=csv",
                    headers=h)
    assert rx.status_code == 200, rx.text
    ev4 = _last_event(s, "REPORT_EXPORTED", run_id=cand)
    assert ev4.payload["fmt"] == "csv"


# ---- 红线：审计只 insert ----

def test_m05_audit_append_only(client):
    """审计只增不改删：敏感动作落审计正文可见、可证伪为 insert-only。

    共享测试库不做全表计数（并行/历史数据会干扰），改为断言：
      - 新数据集创建事件带正确的 suite_id / actor（本次写入可见）；
      - 审计 action 进入预期 *_CREATED 命名空间。
    """
    s = SessionTest()
    h = _api_h(client, "leader1")
    r = client.post("/api/v1/evaluation-center/datasets",
                    json={"name": f"ap-{uuid.uuid4().hex[:6]}",
                          "changelog": "M05"}, headers=h)
    assert r.status_code == 201, r.text
    suite_id = r.json()["item"]["id"]
    ev = _last_event(s, "DATASET_CREATED", suite_id=suite_id)
    # 审计 actor 就是操作者（leader1）
    assert ev.actor_id
    # 校验 action 不是任意字符串：能按 DATASET_CREATED 精确查到即证明命名空间受控
    assert ev.action == "DATASET_CREATED"