# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

架构（docs/06 锁死规格）：
- 受理层（本文件）：鉴权 -> 幂等 -> 落库(RUNNING) -> XADD Redis Stream -> 202 快速返回；
- 决策层：Worker 消费 Stream，驱动 LangGraph 四 Agent 工作流（Phase 4/5 实现）。
"""
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.core.config import settings


def _install_capability_plugin() -> None:
    """API 启动时安装基线四能力插件（capability_table -> available + metric 注册）。

    这样 `/api/v1/evaluation-center/capabilities` 与评测报告如实反映"已实现"，
    无需依赖 worker 进程先启动（测试/部署任一服务先起都成立）。
    """
    try:
        from app.services.capability_plugin import install
        install()
    except Exception:  # noqa: BLE001 —— 插件失败不阻断服务启动
        pass


_install_capability_plugin()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# ---- Prometheus 指标（任务三 3.1：Prometheus/Grafana 实时监控）----
# 低基数设计：按 路由模板+方法+状态码 打标（避免 /refunds/{id} 高基数）
REQ_COUNT = Counter(
    "http_requests_total", "HTTP 请求总数", ["method", "route", "status"]
)
REQ_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP 请求延迟（秒）", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    resp = await call_next(request)
    route = getattr(request.scope.get("route"), "path", request.url.path)
    REQ_COUNT.labels(request.method, route, resp.status_code).inc()
    REQ_LATENCY.labels(request.method, route).observe(time.perf_counter() - start)
    return resp


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus 抓取端点（任务三 3.1）。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# 上传目录（创建工单时按需 mkdir，这里确保存在）
Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

register_exception_handlers(app)
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _api_hosted_services() -> list[str]:
    """本 API 实例托管的能力域（V3-6 独立就绪标记）。

    默认在单体内承载全部六个能力域（api/voice/intent/security/evaluation/sandbox
    均在本进程加载并可服务）；拆分部署时用环境变量 API_READY_SERVICES 收紧声明，
    只标记本实例真实托管的域——不夸大、不把未托管当就绪。
    """
    raw = os.environ.get("API_READY_SERVICES",
                         "api,voice,intent,security,evaluation,sandbox")
    return [s.strip() for s in raw.split(",") if s.strip()]


@app.on_event("startup")
def _mark_startup_readiness() -> None:
    """启动后向就绪注册表声明本实例托管的服务（best-effort，不阻断启动）。"""
    from app.services import readiness as rd

    api_instance = f"api-{os.getpid()}"
    for svc in _api_hosted_services():
        try:
            rd.mark_ready(svc, instance=api_instance)
        except Exception:  # noqa: BLE001 —— 标记失败只记录，服务照常提供
            pass


@app.get("/api/v1/health", tags=["system"])
def health() -> dict:
    """活性探测（liveness）——应用进程存活即 200（Phase 10 语义不变）。"""
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": app.version,
        "features": {"case_evidence_preview": True},
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/health/ready", tags=["system"])
def health_ready() -> dict:
    """就绪探测（readiness）——各能力域独立扩缩容就绪标记（V3-6 ）。

    overall = 所有能力域都有 ≥1 存活实例；services 逐域给 ready/instances。
    只观测编排信号，绝不参与任何退款/审批决策。
    """
    from app.services import readiness as rd

    snap = rd.ready_snapshot()
    snap["status"] = "ready" if snap["overall"] else "not_ready"
    return snap
