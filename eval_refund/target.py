# -*- coding: utf-8 -*-
"""RefundSystemHttpTarget —— 业务主干 退赔评测目标的黑盒 HTTP 适配器。

评测侧不读取被测系统源码/数据库/.env，只通过 HTTP API 交互（与评测体系集成约束一致）：
- 用正式 API 执行完整异步流程：登录 -> 建单 -> 轮询 -> 识别 SUSPENDED ->
  测试 approve/reject -> 读取终态；
- 传递 request_id / case_id / trace_id 三个 Trace 头；
- 目标不可用、超时、协议错误、字段缺失必须明确抛 TargetError（评测判定失败），
  绝不伪造默认值当 pass。

Target 通过既有业务 API 完成工作流（不新增评测专属后门），因此评测
对象与生产对象是同一路径，符合黑盒语义。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx

from .schema import CandidateOutput

# ---- 被测系统默认地址（与 Makefile / runner 对齐）----
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_POLL_MAX_SECONDS = 120.0


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


class TargetError(RuntimeError):
    """目标不可用 / 协议错误 / 响应非法。调用方应视为评测失败而非 pass。"""


class RefundSystemHttpTarget:
    """退赔工作流黑盒目标。"""

    name = "refund_system_http"

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval: float | None = None,
        poll_max_seconds: float | None = None,
        evalcine_runner: str | None = None,
    ) -> None:
        self.base_url = (_env("REFUND_SYSTEM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
                         or DEFAULT_BASE_URL)
        if base_url:
            self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds or float(
            _env("REFUND_SYSTEM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
        self.poll_interval = poll_interval or float(
            _env("REFUND_SYSTEM_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL_SECONDS)))
        self.poll_max = poll_max_seconds or float(
            _env("REFUND_SYSTEM_POLL_MAX", str(DEFAULT_POLL_MAX_SECONDS)))
        self._client: httpx.Client | None = None
        self._token: str | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
            self._client.headers["Accept"] = "application/json"
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- 登录 / 鉴权 ----

    def login(self, username: str, password: str) -> str:
        """登录获取 JWT。失败抛 TargetError（不伪造 token）。"""
        try:
            r = self.client.post(
                f"/api/v1/auth/login",
                json={"username": username, "password": password},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise TargetError(
                f"login 失败 ({exc.__class__.__name__}: {exc}) [base_url={self.base_url}]"
            ) from exc
        if r.status_code != 200:
            raise TargetError(f"login HTTP {r.status_code}: {r.text[:200]}")
        try:
            token = r.json()["token"]
        except (ValueError, KeyError) as exc:
            raise TargetError("login 响应缺少 token") from exc
        self._token = token
        return token

    @property
    def auth_headers(self) -> dict:
        if not self._token:
            raise TargetError("未登录：请先调用 login()")
        return {"Authorization": f"Bearer {self._token}"}

    # ---- Trace 头（：case 关联 request_id / trace_id / eval_case_id）----

    @staticmethod
    def _trace_headers(case_id: str) -> dict:
        return {
            "X-Trace-Id": uuid.uuid4().hex[:32],
            "X-Request-Id": f"eval-{case_id}-{uuid.uuid4().hex[:12]}",
            "X-Eval-Case-Id": case_id,
        }

    # ---- 工作流驱动 ----

    def create_case(
        self,
        order_id: str,
        requested_amount: str,
        refund_reason: str | None = None,
        reason_codes: str | None = None,
        channel: str = "APP",
        evidence_path: str | None = None,
        eval_run_id: str | None = None,
    ) -> str:
        """建单（multipart form + X-Idempotency-Key），返回 case_id。"""
        key = f"eval-{uuid.uuid4().hex[:12]}"
        files: dict[str, tuple[str, bytes, str]] | None = None
        if evidence_path:
            # 评测只读此类文件（黑盒允许：提交附件是真实用户路径）
            with open(evidence_path, "rb") as f:
                content = f.read()
            files = {"evidence": ("evidence.jpg", content, "image/jpeg")}
        data = {
            "order_id": order_id,
            "requested_amount": str(requested_amount),
            "refund_reason": refund_reason or "",
            "channel": channel,
        }
        if reason_codes:
            data["reason_codes"] = reason_codes
        headers = {"X-Idempotency-Key": key}
        if eval_run_id:
            headers["X-Eval-Run-Id"] = eval_run_id
        headers.update(self.auth_headers)
        headers.update(self._trace_headers(order_id))
        try:
            r = self.client.post(
                "/api/v1/refunds", data=data, files=files or None,
                headers=headers, timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise TargetError(f"create_case 失败 ({exc.__class__.__name__}: {exc})") from exc
        if r.status_code != 202:
            raise TargetError(f"create_case HTTP {r.status_code}: {r.text[:300]}")
        try:
            case_id = r.json()["case_id"]
        except (ValueError, KeyError) as exc:
            raise TargetError("create_case 响应缺少 case_id") from exc
        return case_id

    def get_case(self, case_id: str) -> dict:
        """读案件详情（含三态/决策/核验/退款状态）。"""
        headers = dict(self.auth_headers)
        headers.update(self._trace_headers(case_id))
        try:
            r = self.client.get(
                f"/api/v1/refunds/{case_id}", headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise TargetError(f"get_case 失败 ({exc.__class__.__name__}: {exc})") from exc
        if r.status_code != 200:
            raise TargetError(f"get_case HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise TargetError("get_case 响应不是合法 JSON") from exc

    def decide(self, case_id: str, action: str, opinion: str) -> dict:
        """审批/拒绝（主管权限）。action in {approve, reject}。"""
        if action not in ("approve", "reject"):
            raise TargetError(f"非法 action: {action!r}")
        headers = dict(self.auth_headers)
        headers.update(self._trace_headers(case_id))
        try:
            r = self.client.post(
                f"/api/v1/refunds/{case_id}/{action}",
                json={"opinion": opinion}, headers=headers, timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise TargetError(f"decide/{action} 失败 ({exc.__class__.__name__}: {exc})") from exc
        if r.status_code != 202:
            raise TargetError(f"decide/{action} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError as exc:
            raise TargetError("decide 响应不是合法 JSON") from exc

    # ---- 终态轮询 ----

    def wait_terminal(
        self, case_id: str,
        decision: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        """轮询直到 COMPLETED（或指定的终态决策）。超时抛 TargetError（不是 pass）。

        若目标选择挂起（SUSPENDED），调用方通过 decide() 唤醒后再 wait。本方法只
        负责等稳定终态；不把中途状态误判为终态。
        """
        deadline = time.monotonic() + (timeout_seconds or self.poll_max)
        rounds = 0
        last: dict = {}
        while time.monotonic() < deadline:
            rounds += 1
            c = self.get_case(case_id)
            last = c
            status = c.get("current_status")
            if status == "COMPLETED":
                if decision is None or c.get("final_decision") == decision:
                    c["_polling_rounds"] = rounds
                    return c
                # 达到 COMPLETED 但决策不符 -> 明确报错，不静默失败
                raise TargetError(
                    f"案件已终态但决策不符: got={c.get('final_decision')} want={decision} "
                    f"case_id={case_id}")
            time.sleep(self.poll_interval)
        raise TargetError(
            f"轮询超时（{timeout_seconds or self.poll_max}s / {rounds} 轮）: case_id={case_id} "
            f"最后状态={last.get('current_status')}/{last.get('final_decision')}")

    # ---- 运行单条评测 ----

    def run_case(self, golden: dict) -> CandidateOutput:
        """执行一条 Golden：按 case 驱动完整工作流，把终态规范化为 CandidateOutput。

        只支持确定性流程：目标要么自动到终态，要么挂起后由本方法按 golden 的
        expected_decision 唤醒。boundary/故障类用例由调用方（runner/judge）按各自
        semantics 处理，不经本方法触发副作用。
        """
        case_id = str(golden["case_id"])
        body = golden.get("body") or {}
        # 登录（golden 可显式指定角色）
        username = body.get("submit_username") or "customer_service1"
        password = body.get("submit_password") or "123456"
        self.login(username, password)
        cid = self.create_case(
            order_id=body["order_id"],
            requested_amount=str(body["requested_amount"]),
            refund_reason=body.get("refund_reason"),
            reason_codes=body.get("reason_codes"),
            eval_run_id=golden.get("eval_run_id"),
        )
        # 轮询到 CAN 终态（COMPLETED）或需唤醒的挂起
        poll_timeout = float(body.get("poll_seconds") or self.poll_max)
        deadline = time.monotonic() + poll_timeout
        rounds = 0
        c: dict = {}
        while time.monotonic() < deadline:
            rounds += 1
            c = self.get_case(cid)
            if c.get("current_status") == "COMPLETED":
                break
            if c.get("current_status") == "SUSPENDED":
                # 需要人工唤醒：golden 声明 expected_decision 才可唤醒
                expected = (golden.get("expected") or {}).get("final_decision")
                if expected == "APPROVED":
                    self.login(body.get("approver_username") or "supervisor1",
                               body.get("approver_password") or "123456")
                    self.decide(cid, "approve", "评测用例人工审批")
                elif expected == "REJECTED":
                    self.login(body.get("approver_username") or "supervisor1",
                               body.get("approver_password") or "123456")
                    self.decide(cid, "reject", "评测用例人工拒绝")
                else:
                    # 无声明决策（如"应保持挂起"）-> 终止轮询，报告挂起态
                    break
                time.sleep(self.poll_interval)
                continue
            time.sleep(self.poll_interval)
        if c.get("current_status") not in ("COMPLETED", "SUSPENDED"):
            raise TargetError(
                f"用例 {case_id} 未能到达终态: {c.get('current_status')}/{c.get('final_decision')} "
                f"({rounds} 轮)")

        out = self._to_candidate(c, rounds, cid)
        # 清理：评测数据隔离（M8 在服务端按 eval_run_id 过滤，不在此删生产数据）
        return out

    def _to_candidate(self, case_detail: dict, rounds: int, case_id: str) -> CandidateOutput:
        """CaseDetail -> CandidateOutput（规范化后 Judge 只读本结构）。"""
        out = CandidateOutput(
            case_id=case_id,
            trace_id=str(case_detail.get("trace_id") or ""),
            current_status=str(case_detail.get("current_status") or "UNKNOWN"),
            final_decision=str(case_detail.get("final_decision") or "PENDING"),
            refund_status=str(case_detail.get("refund_status") or "NONE"),
            current_graph_node=str(case_detail.get("current_graph_node") or ""),
            order_verification=str(case_detail.get("order_verification") or "UNKNOWN"),
            product_consistency=str(case_detail.get("product_verification") or "UNKNOWN"),
            price_consistency=str(case_detail.get("price_verification") or "UNKNOWN"),
            http_status=200,
            polling_rounds=rounds,
        )
        out.validate()
        return out