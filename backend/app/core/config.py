# -*- coding: utf-8 -*-
"""应用配置：全部从环境变量 / .env 读取，禁止硬编码密钥。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    APP_NAME: str = "多Agent客诉舆情退赔决策系统"
    APP_VERSION: str = "3.0.0"
    DEBUG: bool = False

    # ---- 扩展能力配置开关（基线 V1.0 默认全开；V2.0 接真引擎时可关）----
    CAPABILITY_BASELINE_ENABLED: bool = True   # 关闭时 capabilities 回退 unavailable/SKIPPED（dir 评测入口）

    # ---- MySQL（裁决 D3）----
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""          # 开发实例 root 空密码；生产用环境变量注入
    DB_NAME: str = "complaint_refund"

    # ---- Redis（D:\Redis 服务，端口 6379）----
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # ---- JWT ----
    JWT_SECRET: str = "dev-only-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 120

    # ---- LLM（裁决 D2：云 API 主 + Ollama 回退）----
    LLM_API_BASE: str = "https://api.agicto.cn/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-v4-pro"
    LLM_TIMEOUT_SECONDS: float = 10.0
    LLM_FALLBACK_MODEL: str = "qwen2:7b"          # Ollama
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    # 演示/离线环境可启用：云 LLM 与 Ollama 均不可用时，使用可解释关键词规则；
    # 生产默认关闭，继续执行保守 HIGH -> 人工审核策略。
    SENTIMENT_RULE_FALLBACK_ENABLED: bool = False

    # ---- 多模态凭证理解（传统 OCR + 本地 Qwen2.5-VL）----
    VISION_ENABLED: bool = True
    VISION_MODEL: str = "qwen2.5vl:7b"
    # 实测热启动多为 4~10 秒，但本地服务可能卡住；超时后降级 OCR + 人工复核。
    # 7B 视觉模型在 8GB 显存笔记本上冷启动明显慢于热推理；超时后工作流会降级而非卡死。
    VISION_TIMEOUT_SECONDS: float = 180.0
    VISION_KEEP_ALIVE: str = "30m"
    VISION_CONTEXT_TOKENS: int = 2048
    VISION_MAX_OUTPUT_TOKENS: int = 220
    VISION_MAX_IMAGE_EDGE: int = 384
    VISION_MIN_CONFIDENCE: float = 0.6
    VISION_BREAKER_FAIL_THRESHOLD: int = 3
    VISION_BREAKER_OPEN_SECONDS: float = 300.0

    # ---- 意图周期评测 ----
    # 默认每日执行固定 120 条离线回放，不产生云端模型费用。
    # 仅在明确配置为 true 时，对照评测才调用云/Ollama LLM 网关。
    INTENT_EVAL_AUTO_SCHEDULE_ENABLED: bool = True
    INTENT_EVAL_USE_LIVE_LLM: bool = False

    # ---- 业务阈值默认值（运行期以 system_configs 表为准）----
    AMOUNT_THRESHOLD: float = 300.0
    FRAUD_SUSPEND_THRESHOLD: int = 60
    FRAUD_AUTO_PASS_THRESHOLD: int = 20
    OCR_CONFIDENCE_THRESHOLD: float = 0.6

    # ---- 幂等 / 锁 ----
    IDEMPOTENCY_TTL_SECONDS: int = 86400           # 24h
    APPROVE_LOCK_TTL_SECONDS: int = 10

    # ---- 文件上传 ----
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_SIZE_MB: int = 5
    ALLOWED_IMAGE_EXT: tuple = (".jpg", ".jpeg", ".png", ".webp")

    # ---- 语音会话（基线 Mock VAD/STT/TTS，不接真 WebRTC/引擎）----
    VOICE_MOCK_ENABLED: bool = True          # 基线全 Mock；V2.0 换真引擎时置 False
    VAD_SILENCE_MS: int = 1500               # 用户语音结束静音判据（1.5s 静音断点）
    VOICE_INTERRUPT_MS: int = 100            # 插话检测窗口：TTS 播放中 100ms 内出现输入即打断
    VOICE_SESSION_TTL_SECONDS: int = 300     # 会话超时回收（防僵尸连接）
    VOICE_SESSION_MAX_SECONDS: int = 600     # 单会话最长时长
    VOICE_MAX_FRAME_SIZE: int = 4096         # 单帧 PCM 上限（字节）
    VOICE_CREATE_TEST_AMOUNT_YUAN: float = 99.0   # 语音测试工单默认金额
    # V2.0 -5：并发/重连/超时能力
    VOICE_MAX_CONCURRENT: int = 32           # 同进程最大并发语音会话（V3-5：30-40 路区间，默认 32）
    VOICE_IDLE_TIMEOUT_SECONDS: float = 60.0 # 单会话静音空闲超时（超时置 closed_reason=timeout 回收）

    # ---- V3-5 生产级实时语音：链路时延预算 + 队列/熔断/降级 ----
    # 预算：VAD 断点 ≤150ms / STT ≤200ms / LLM 首 Token ≤300ms / TTS 首包 ≤200ms
    VOICE_VAD_BUDGET_MS: int = 150
    VOICE_STT_BUDGET_MS: int = 200
    VOICE_LLM_FIRST_TOKEN_BUDGET_MS: int = 300
    VOICE_TTS_FIRST_FRAME_BUDGET_MS: int = 200
    # 限流排队：容量超限 -> 进入队列等待而非直接 429（满队列才 DROP）
    VOICE_QUEUE_CAPACITY: int = 32            # 排队等待上限（超过视为系统过载 DROP）
    VOICE_QUEUE_TIMEOUT_SECONDS: float = 5.0  # 排队最长等待（超时回 429 busy）
    # Provider 熔断降级：连续失败 N 次 -> 熔断，冷却后半开窗口试恢复
    VOICE_BREAKER_FAIL_THRESHOLD: int = 5
    VOICE_BREAKER_COOLDOWN_SECONDS: float = 10.0
    # 业务推理优先级窗口：离线评测/陪练与实时业务推理同源，但实时业务推理优先调度
    VOICE_PRIORITY_BUSINESS_FIRST: bool = True

    # ---- 客服陪练 ----
    COACHING_SCENE_REFUND: str = "VOICE_SCENE_REFUND"   # 唯一客诉退赔场景常量
    COACHING_MAX_TURNS: int = 12                         # 单会话最大回合（防死循环）

    # ---- V3.0：多来源核验信任 + 对账窗口 ----
    ORDER_SOURCE_TRUST: dict = {"mock-a": 1.0, "mock-b": 0.6}  # 来源信任权重（多来源汇聚投票）
    RECONCILE_WINDOW_MINUTES: int = 60                          # 自动对账默认时间窗（近 N 分钟案件）
    RECON_SOURCE_BREAKER_FAILS: int = 2                         # 连续失败 N 次 -> 熔断该来源
    RECON_SOURCE_BREAKER_COOLDOWN_SECONDS: float = 5.0          # 熔断冷却（半开窗口）

    # 商城/外部订单快照：API 与 Worker 跨进程共享，过期后必须重新从订单源同步。
    EXTERNAL_ORDER_TTL_SECONDS: int = 30 * 24 * 60 * 60


settings = Settings()
