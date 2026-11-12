# -*- coding: utf-8 -*-
"""Locust 压测脚本（任务三，docs/06 D7/D10）
目标：QPS≥200 / P95<300ms / 错误率<0.1%

运行：cd tools && locust -f locustfile.py --host http://127.0.0.1:8001
任务比例：列表 10 / 创建 5 / 详情 5 / 统计 2 / 审批 1
说明：创建返回 202（异步受理），worker 后台消化；审批/详情从预置案件池取数。
"""
import os
import random
import uuid

import requests
from locust import HttpUser, between, events, task

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = os.path.join(BASE_DIR, "locust_evidence.jpg")

# 预置案件池：启动时拉取，供详情/审批使用
CASE_POOL = []


class RefundUser(HttpUser):
    """客服工作台典型操作序列：列表→详情→新建→统计→审批"""
    wait_time = between(0.05, 0.2)

    def on_start(self):
        r = self.client.post(
            "/api/v1/auth/login",
            json={"username": "supervisor1", "password": "123456"},
        )
        if r.status_code == 200:
            self.token = r.json()["token"]
        else:
            self.token = None

    def _headers(self, **extra):
        h = {"Authorization": f"Bearer {self.token}"}
        h.update(extra)
        return h

    @task(10)
    def list_cases(self):
        self.client.get(
            "/api/v1/refunds?page=1&size=20",
            headers=self._headers(),
            name="GET /refunds (list)",
        )

    @task(5)
    def create_refund(self):
        """新建退款申请：multipart + 幂等键（202 异步受理）"""
        with open(EVIDENCE, "rb") as f:
            files = {"evidence": ("ev.jpg", f, "image/jpeg")}
            data = {
                "order_id": f"PERF-{uuid.uuid4().hex[:8].upper()}",
                "requested_amount": str(random.choice([88, 128, 168, 228, 350, 520])),
                "refund_reason": "压测样本",
            }
            self.client.post(
                "/api/v1/refunds",
                data=data,
                files=files,
                headers=self._headers(
                    **{"X-Idempotency-Key": str(uuid.uuid4())}
                ),
                name="POST /refunds (create)",
            )

    @task(5)
    def case_detail(self):
        if not CASE_POOL:
            return
        cid = random.choice(CASE_POOL)
        self.client.get(
            f"/api/v1/refunds/{cid}",
            headers=self._headers(),
            name="GET /refunds/{id} (detail)",
        )

    @task(2)
    def dashboard_stats(self):
        self.client.get(
            "/api/v1/dashboard/stats",
            headers=self._headers(),
            name="GET /dashboard/stats",
        )

    @task(1)
    def approve_case(self):
        """主管审批：409（非挂起/重复审批）为业务预期拒绝，不计入失败；仅 5xx/超时算失败"""
        if not CASE_POOL:
            return
        cid = random.choice(CASE_POOL)
        with self.client.post(
            f"/api/v1/refunds/{cid}/approve",
            data={"opinion": "情况属实，批准退款"},
            headers=self._headers(
                **{"X-Idempotency-Key": str(uuid.uuid4())}
            ),
            name="POST /refunds/{id}/approve",
            catch_response=True,
        ) as resp:
            if resp.status_code in (202, 200, 409):
                resp.success()
            else:
                resp.failure(f"unexpected status {resp.status_code}")


def _prime_case_pool(host, token, count=60):
    """压测前预置案件池：批量创建小额案件（多为自动完成）+ 中额案件（挂起供审批）"""
    import requests

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    created = []
    for _ in range(count):
        with open(EVIDENCE, "rb") as f:
            r = s.post(
                f"{host}/api/v1/refunds",
                data={
                    "order_id": f"PERF-PRIME-{uuid.uuid4().hex[:8].upper()}",
                    "requested_amount": str(random.choice([188, 350, 520])),
                    "refund_reason": "压测预置",
                },
                files={"evidence": ("ev.jpg", f, "image/jpeg")},
                headers={"X-Idempotency-Key": str(uuid.uuid4())},
                timeout=10,
            )
        if r.status_code == 202:
            created.append(r.json()["case_id"])
    return created


# 启动钩子：登录 + 预置案件池（异步，不阻塞 worker 启动）
@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    import threading
    import time

    def prime():
        host = environment.host
        # 预置完成后把案件 ID 填进全局池
        global CASE_POOL
        r = requests.post(
            f"{host}/api/v1/auth/login",
            json={"username": "supervisor1", "password": "123456"},
            timeout=10,
        )
        if r.status_code != 200:
            print("[prime] login failed, pool empty")
            return
        CASE_POOL = _prime_case_pool(host, r.json()["token"])
        print(f"[prime] case pool ready: {len(CASE_POOL)} cases")

    # 等 3 秒让 worker 先稳定，再预置
    time.sleep(3)
    threading.Thread(target=prime, daemon=True).start()
