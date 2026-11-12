# -*- coding: utf-8 -*-
"""Redis 客户端：Streams / 幂等键 / 分布式锁 / Checkpointer 统一出口。

V3-6（-高可用）：连接池 HA + 自动重试，Checkpointer/幂等/锁不再被单点抖动打穿。

- 连接池：max_connections + health_check_interval + socket 超时（防半死连接卡住主流程）；
- 自动重试：ConnectionError/TimeoutError 指数退避重试（REDIS_RETRIES 次），
  瞬时抖动透明恢复；真实不可达在重试后按原语义抛出（绝不静默吞掉 Streams 投递）；
- `redis_health()`：健康探测（供 /health/ready 聚合与巡检）；
- `safe_redis()`：显式降级包装 —— 只在调用方明确要求「Redis 挂了就降级」的旁路场景用
  （Trace 上报、就绪标记等），故障窗口内短路返回 default，避免重复连接风暴；
  退款/幂等/锁主路径不走它（那些路径的失败必须如实暴露）。
"""
import time
import redis as redis_py
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from app.core.config import settings

# 重试次数（指数退避）；超出后在调用侧面如实抛出
REDIS_RETRIES = 3
REDIS_MAX_CONNECTIONS = 32
# 故障窗口：Redis 真宕机时 safe_redis 短路回 default 的时长（秒）
SAFE_FAIL_WINDOW_SECONDS = 1.0

# 连接错误集合：重试/降级统一口径
_RETRY_ERRORS = (redis_py.ConnectionError, redis_py.TimeoutError, OSError)


def _build_client(decode_responses: bool) -> redis_py.Redis:
    """带连接池 HA 的统一构建：重试/超时/心跳探测，单点抖动自愈。"""
    return redis_py.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=decode_responses,
        max_connections=REDIS_MAX_CONNECTIONS,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
        socket_keepalive=True,
        health_check_interval=30,
        retry=Retry(ExponentialBackoff(base=0.05, cap=0.5), REDIS_RETRIES),
        retry_on_timeout=True,
    )


redis_client = _build_client(decode_responses=True)

# pickle 二进制连接（Checkpointer 用）也走同一 HA 配置 —— 单点故障以「重试→如实失败」
# 语义暴露，由 caller（redis_checkpointer 安全失败/上游转人工）兜底。
binary_redis_client = _build_client(decode_responses=False)

STREAM_REFUND_APPLY = "stream:refund_apply"
STREAM_HUMAN_DECISION = "stream:human_decision"
STREAM_DLQ = "stream:dlq"
STREAM_EVAL_RUN = "stream:eval_run"   # 评测中心异步 Run（M12）
STREAM_TRACE_APPEND = "stream:trace_append"   # V3-6 Trace 上报通道（旁路，绝不阻塞主流程）


def xadd(stream: str, fields: dict) -> str:
    """写入消息（不阻塞，快速返回）。失败如实抛出（主流程投递不可静默丢弃）。"""
    return redis_client.xadd(stream, fields)


# ---- V3-6 高可用：健康探测 + 显式降级 ----

_last_fail_ts: float = 0.0


def redis_health() -> dict:
    """活性探测（幂等可并行；供健康聚合/巡检，不做业务判定）。"""
    try:
        ok = bool(redis_client.ping())
        return {"redis": "UP" if ok else "DOWN", "ok": ok}
    except Exception as exc:  # noqa: BLE001 —— 探测失败如实 DOWN
        return {"redis": "DOWN", "ok": False, "error": str(exc)[:120]}


def safe_redis(fn, default=None, *args, **kwargs):
    """显式降级包装：连接异常 -> 返回 default（故障窗口内短路，过窗口自动重试探测）。

    仅用于「Redis 挂掉也能降级继续」的旁路场景（Trace 上报、就绪标记）。
    退款/幂等/锁主路径**禁止**用本包装（投递失败必须如实暴露给调用方处置）。
    """
    global _last_fail_ts
    now = time.monotonic()
    if _last_fail_ts and (now - _last_fail_ts) < SAFE_FAIL_WINDOW_SECONDS:
        return default
    try:
        return fn(*args, **kwargs)
    except _RETRY_ERRORS:
        _last_fail_ts = time.monotonic()
        return default