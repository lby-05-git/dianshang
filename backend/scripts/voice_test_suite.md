# 语音专项测试集（V3-5）

本文件是**语音识别/对话链路专项用例集**（非回归单测，作为压测与人工/自动化对照集）。
覆盖语音落地最易出错的 5 类场景。每条给出：构造方法、期望、红线约束。

> 红线：所有语音用例只建 **EVAL 打标测试工单**，绝不触发真实退款/审批；
> turns 落库前脱敏；仅存 `intent_raw_sha256` 等摘要，绝不明文落意图原文。

跑法（专项手动对照，供评测转自动化时对齐）：
```
PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe scripts/voice_load_test.py [并发]
```

---

## 1. 语音识别错误（ASR 误识 / 关键信息缺失）

| # | 场景 | 构造 | 期望 |
|---|------|------|------|
| E1 | 订单号误识（OR 混 O） | `stt: "订单 ORD-OO1234 金额 88 元"` | MockSTT 提取 `ORD-OO1234`，回复回显该订单号；**不静默改单** |
| E2 | 金额单位缺失 | `stt: "订单 ORD-X1 我要退款"` | 金额为 None -> 回复「请您提供订单号与退款金额」；不建单 |
| E3 | 金额多零 / 超上限 | `amount=100000.01`（create-test-case） | 表单校验拒绝（≤100000），HTTP 422 |
| E4 | 无关键信息兜底 | `stt: "你好"` | 兜底确认文案，`order_id/amount` 为 None，不建单 |

**红线核对**：E2/E4 不落任何 RefundCase；E1 建单金额与回显一致（金额打码防侧录）。

## 2. 错误打断（TTS 播报中误触发中断）

| # | 场景 | 构造 | 期望 |
|---|------|------|------|
| I1 | 正常打断 | 发送 `interrupt` 一次 | `tts_cleared>=1`、状态 `INTERRUPTED`、TTS 队列清空 |
| I2 | 连打（系统噪声） | 1s 内连发 3 次 `interrupt` | 第一次生效（清队），后续幂等计数累加；不状态错乱 |
| I3 | 打断后恢复 | interrupt 后继续 `stt` | 状态回到可识别流，回复引用新文本（不串旧 TTS） |
| I4 | 打断延迟预算 | 从发 `interrupt` 到收 `interrupt` 事件 | ≤100ms（压测脚本已判定） |

## 3. 串音 / 串 Trace / 串上下文

| # | 场景 | 构造 | 期望 |
|---|------|------|------|
| X1 | 多路并发不串音 | 32-40 路并发 `voices`，各自独立 session_id | 每会话事件序一致、session_id 两两独立 |
| X2 | 不串 Trace | 每会话发独立 stt 文本，回收后核对 | 各 session 的 trace 仅含本会话事件；LATENCY 落其自身 session_id |
| X3 | 降级 Provider 不串 | 熔断打开下业务会话并发 | fallback 会话入注册表，结束全回收（active=0） |
| X4 | 断线重连不串 | `reconnect` 2 次 | reconnects=2 落库，会话状态不被打断 |

## 4. 弱网

| # | 场景 | 构造 | 期望 |
|---|------|------|------|
| W1 | 弱网标记 | 发 `weak` 信令 | 会话落 `weak_signal=1` |
| W2 | 网络抖动重连 | `reconnect` 多次后继续 `stt` | 会话继续工作（业务状态保持），reconnects 累加 |
| W3 | 长静音弱网 | 静默超过 `VOICE_IDLE_TIMEOUT` | close reason=timeout + ERROR 遥测，无退款 |
| W4 | 弱网下插话 | weak 后立即 interrupt | 打断仍即时生效（前后端互不阻塞） |

## 5. 高噪声 / 音频异常

| # | 场景 | 构造 | 期望 |
|---|------|------|------|
| N1 | 超帧长音频 | 单帧 > VOICE_MAX_FRAME_SIZE(4096) | 直接丢弃（不崩、不建异常单） |
| N2 | 纯噪声（随机字节） | 无静音连续 feed | 不触发 vad_end；不产生业务副作用 |
| N3 | 噪声+有效语音 | 噪声帧后接真实 stt | 关键信息正常提取，与噪声无关 |
| N4 | 高并发+噪声混合 | 压测中加入若干噪声帧 | 并发统计仍达标，无串音 |

---

## 量化门禁（V3-5 验收对照 ）

| 指标 | 目标 | 判定来源 |
|------|------|---------|
| 实时并发 | 30-40 路 | `voice_load_test.py -n 32/40` |
| 端到端首响应 | P95 ≤ 800ms | 压测脚本首响应 P95 |
| 插话控制 | ≤ 100ms | 压测脚本 interrupt 往返 P95 |
| VAD 断点 | ≤ 150ms | `voice_metrics.pipeline_budget.VAD_END` |
| STT | ≤ 200ms | `pipeline_budget.STT` |
| LLM 首 Token | ≤ 300ms | `pipeline_budget.LLM_FIRST` |
| TTS 首包 | ≤ 200ms | `pipeline_budget.TTS_FIRST` |
| 无真实退款 | = 0 | 全用例 `RefundTransaction` 计数不增 |