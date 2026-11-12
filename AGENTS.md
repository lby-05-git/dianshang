# AGENTS.md

## 1. 当前项目定位

本仓库是 V3.0 融合需求文档对应的实现，覆盖以下能力域：

- 业务主干：客诉受理、订单核验、OCR、欺诈、舆情、HITL、退款与审计事实；
- 评测与成本：Agent评测、成本治理、Prompt A/B与Office安全沙箱；
- 安全治理：Critic、DLP、工具安全网关、安全事件与零信任版本治理；
- 意图与可靠性：多级意图路由、多意图冲突、反馈采样、异常修复与死信队列；
- 实时交互：实时语音、容量治理、断线恢复和客服陪练。

产品版本为3.0.0。后续修改以真实代码为准，不再使用“部分能力仅预留、不得实现”的旧阶段口径。

## 2. 目录职责

| 目录/文件 | 职责 |
|---|---|
| backend/app/ | FastAPI、领域模型、LangGraph、Worker、安全、意图、语音与治理服务 |
| backend/tests/ | 后端回归、安全红线、故障恢复与性能口径测试 |
| backend/alembic/versions/ | 仅追加式数据库迁移 |
| eval_refund/ | EvaluationTarget、Golden、Judge、Runner及能力评测 |
| tests/eval_refund/ | 评测单元与契约测试 |
| frontend/ | Vue 3产品工作台，覆盖业务、评测、安全、意图、语音和发布治理 |

## 3. 不可触犯的安全红线

1. 不得调用真实退款渠道；评测、语音测试和陪练只能使用Mock/EVAL隔离适配器。
2. 不得修改真实密钥、.env凭据，或绕过JWT、RBAC、HITL与工具安全网关。
3. 不得删除、篡改、跳过既有测试来制造通过结果。
4. 未实际测量的Token、时延、成本、召回率、正确率不得写成已达标；必须标记measured=false、None或PENDING。
5. 订单、商品或价格核验为MISMATCH/UNKNOWN时必须挂起转人工，禁止自动退款。
6. 挂起前必须保存langgraph_thread_id检查点；恢复后不得重复执行退款等外部副作用。
7. 工单状态、业务决策和退款状态必须独立；refund_status=FAILED时禁止展示退款成功。
8. 评测、陪练与业务数据必须隔离；清理数据只能按明确的Run/EVAL标识执行。
9. 沙箱禁止shell=True，命令使用参数数组；实例必须在正常、异常和超时路径中回收。
10. 异步并行工具调用使用asyncio.gather(..., return_exceptions=True)，局部失败不得拖垮整链。
11. 异常不得静默吞掉；必须写入Trace/降级队列并向上层反馈。
12. 安全服务故障时，资金副作用必须Fail-Closed；普通只读查询只能按批准策略降级。
13. security/intent/voice/coaching只有在真实插件注册和实测证据存在时才能标记available/PASS。

## 4. 开发与协作规则

- 修改前先核对现有实现和相关测试，保留用户已有改动。
- 数据库变更只允许新增迁移；不得修改已应用迁移历史。
- 每个功能遵循“失败测试→最小实现→运行闸门→复核证据”的闭环。
- API和前端必须使用同一真实契约；禁止用静态假数据伪装后端能力。
- 前端使用frontend/src/api.js统一处理JWT和401；高风险操作必须有明确人工确认。
- 版本、评测、对账和安全处置只写各自治理事实，不得静默改写退款业务事实。

## 5. 提交前质量闸门

Windows运行check.bat；Linux、macOS或Git Bash运行make check。

闸门包含：

1. backend/tests后端全量回归；
2. tests/eval_refund评测契约测试；
3. Vue前端生产构建。

专项验证按需运行：

- 全量评测：.venv/Scripts/python -m eval_refund.runner --limit 0
- LLM Judge：.venv/Scripts/python -m eval_refund.runner --limit 0 --judge
- 红蓝对抗：backend/scripts/red_blue_test.py
- 语音压测：backend/scripts/voice_load_test.py

## 6. 运行环境

- Python：仓库根目录.venv（Python 3.12）。
- 后端：在backend/执行uvicorn app.main:app --port 8001 --reload。
- Worker：在backend/执行python -m app.worker.run。
- 前端：在frontend/执行npm run dev，默认代理到127.0.0.1:8001。
- 数据库迁移：在backend/执行alembic upgrade head。

可信运行积攒、真实意图语料指标和生产订单/支付渠道仍属于部署与真实数据阶段；没有真实证据时必须继续显示PENDING/未测量。
