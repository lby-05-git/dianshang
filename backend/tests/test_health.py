# -*- coding: utf-8 -*-
"""Phase 1 验证：应用可导入、健康检查可用（不需要启动服务器）。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "time" in body
