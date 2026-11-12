# 智诉决策平台 · 多 Agent 协同客诉舆情退赔决策系统

[![Python](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)](https://fastapi.tiangolo.com/)
[![Vue.js](https://img.shields.io/badge/Vue-3-42B883)](https://vuejs.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-Workflow-2B5B84)](https://langchain-ai.github.io/langgraph/)
![Status](https://img.shields.io/badge/Status-Internal%20Workbench-5F6368)

> 面向电商售后风控与运营团队的**内部决策工作台（B 端）**：把客诉受理、订单核验、欺诈与舆情识别、退款裁决、人工介入（HITL）与全链路审计，编排为可追溯、可评测、可治理的多智能体协同流程。

**边界声明**：本系统不是面向消费者的自助客服产品，也不是可直接投产的资金系统。所有涉及资金的操作均通过 Mock / EVAL 隔离适配器执行；未实测指标统一标记 `PENDING`，不以「已达标」表述。

## 目录

- [一、项目定位与适用边界](#sec-overview)
- [二、核心设计原则](#sec-principles)
- [三、能力矩阵](#sec-capabilities)
- [四、技术栈与架构分层](#sec-stack)
- [五、快速开始](#sec-quickstart)
- [六、目录结构](#sec-layout)
- [七、质量闸门](#sec-quality)
- [八、安全红线](#sec-redlines)
- [九、文档索引](#sec-docs)
- [十、当前状态与未决项](#sec-status)

---

<a id="sec-overview"></a>

## 一、项目定位与适用边界

一笔客诉退赔的难点不在「退不退」，而在于**证据链是否闭合、风险是否识别、责任是否可追溯**。系统将原本依赖人工经验的判断过程拆解为多个专职 Agent 协同完成，并在关键风险点强制挂起转人工，避免模型幻觉导致资金损失。

| 维度 | 说明 |
|---|---|
| 产品形态 | B 端内部决策工作台（Web 工作台 + 后端服务），供售后风控、运营与评测人员使用 |
| 核心目标 | 退赔决策的证据链闭合、风险可识别、责任可追溯，关键风险点强制 HITL |
| 适用场景 | 内部演示、方案评审、能力评测与工程验证 |
| 不适用场景 | 生产环境真实退款、面向消费者的自助客服、未经评测的真实资金链路 |
| 资金操作 | 一律通过 Mock / EVAL 隔离适配器执行，禁止调用真实退款渠道 |
| 数据口径 | 未实测的 Token、时延、成本、召回率、正确率标记 `measured=false` / `PENDING` |

---

<a id="sec-principles"></a>

## 二、核心设计原则

| 原则 | 工程约束 |
|---|---|
| **Fail-Closed** | 安全服务故障时，涉及资金的副作用一律拒绝执行，而不是放行 |
| **证据优先** | 订单、商品、价格核验为 `MISMATCH` / `UNKNOWN` 时，必须挂起转人工，**禁止自动退款** |
| **状态解耦** | 工单状态、业务决策、退款状态三者相互独立；`refund_status=FAILED` 时绝不展示「退款成功」 |
| **诚实标注** | 未实测指标一律标记 `measured=false` / `PENDING`，不写「已达标」 |

---

<a id="sec-capabilities"></a>

## 三、能力矩阵

| 能力域 | 落地要点 |
|---|---|
| 业务主干 | 客诉受理、订单核验、OCR 凭证识别、欺诈评分、舆情分级、HITL 挂起/恢复、退款执行、审计事实 |
| 评测与成本 | Agent 评测体系、成本治理、Prompt A/B 实验、Office 安全沙箱 |
| 安全治理 | LLM Critic、NER/DLP 脱敏、工具安全网关、安全事件、零信任版本治理 |
| 意图与可靠性 | 多级意图路由、多意图冲突消解、反馈采样、异常修复、死信队列 |
| 实时交互 | 实时语音、容量治理、断线恢复、客服陪练 |

---

<a id="sec-stack"></a>

## 四、技术栈与架构分层

| 层 | 技术选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy 2.0 · Alembic · MySQL 8.0 · Redis |
| 编排 | LangGraph（+ Redis Checkpointer） |
| 安全与集成 | JWT（python-jose）· bcrypt · prometheus-client · rapidocr-onnxruntime |
| 前端 | Vue 3 · Vue Router 4 · Axios · ECharts · Vite 5 |
| 评测 | 自建 `eval_refund`：Golden 数据集 + 规则判定 + 可选 LLM Judge，支持 A/B 对照与能力套件 |
| 工程 | Docker / Docker Compose（后端、Worker、前端、MySQL、Redis 一编排起）· Prometheus 指标暴露 |

请求经 `api/` 路由进入 `workflow/` 的 LangGraph 状态图，由 `agents/` 定义决策策略，`services/` 提供安全网关、DLP、Critic、意图路由等横切能力，`infrastructure/` 统一管理 DB / Redis / OCR / LLM 与退款适配器；异步任务由 `worker/` 消费 Redis Stream。评测框架独立于业务链路，避免评测逻辑影响业务决策。

---

<a id="sec-quickstart"></a>

## 五、快速开始

### 前置条件

| 依赖 | 版本 / 说明 |
|---|---|
| Python | 3.12（推荐仓库根目录 `.venv`） |
| MySQL | 8.0 |
| Redis | 本地或容器实例 |
| Node.js | 18+ |

### 1. 配置环境变量

```bash
cp backend/.env.example backend/.env
```

编辑 `backend/.env`，至少填入：

```ini
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=complaint_refund

REDIS_URL=redis://127.0.0.1:6379/0

JWT_SECRET=           # 生产环境务必换成随机长字符串
LLM_API_KEY=          # 云 LLM 密钥，切勿提交到 git
LLM_MODEL=deepseek-v4-pro
```

> ⚠️ `.env` 已被 `.gitignore` 排除。本项目为**公开仓库**，请务必确认密钥、令牌不进入版本库 —— 一旦推送，即使后续删除也能从 git 历史中恢复。

### 2. 初始化数据库

```bash
cd backend
alembic upgrade head
```

### 3. 启动服务

```bash
# 后端 API（端口 9001）
cd backend
uvicorn app.main:app --port 9001 --reload

# 异步 Worker（新开终端）
cd backend
python -m app.worker.run

# 前端（默认代理到 127.0.0.1:9001）
cd frontend
npm install
npm run dev
```

也可以直接用 Docker Compose 拉起全套环境：

```bash
docker compose up -d
```

或 Windows 下一键启动：`start_all.bat` / `start_all.ps1`。

---

<a id="sec-layout"></a>

## 六、目录结构

```
.
├─ backend/
│  ├─ app/
│  │  ├─ api/            # FastAPI 路由（auth / orders / refunds / eval / security / voice ...）
│  │  ├─ domain/         # 领域模型与状态机
│  │  ├─ agents/         # 多 Agent 定义与决策策略
│  │  ├─ workflow/       # LangGraph 图编排 + Redis Checkpointer
│  │  ├─ services/       # 安全网关 / DLP / Critic / 意图路由 / 语音 / 治理 等 30+ 服务
│  │  ├─ infrastructure/ # DB / Redis / OCR / LLM / 退款适配器
│  │  └─ worker/         # Stream 消费者
│  ├─ alembic/           # 数据库迁移（仅追加，不得修改已应用历史）
│  ├─ scripts/           # 红蓝对抗、语音压测等专项脚本
│  └─ tests/             # 后端回归、安全红线、故障恢复测试
├─ frontend/             # Vue 3 工作台（业务 / 评测 / 安全 / 意图 / 语音 / 治理）
├─ eval_refund/          # 评测框架：Target / Golden / Judge / Runner / 能力套件
├─ ui/                   # 界面与流程截图
└─ tests/eval_refund/    # 评测单元与契约测试
```

各子目录的本地开发细节见 [`backend/README.md`](backend/README.md) 与 [`frontend/README.md`](frontend/README.md)。

---

<a id="sec-quality"></a>

## 七、质量闸门

提交前请通过全部门槛：

```bash
# Windows
check.bat

# Linux / macOS / Git Bash
make check
```

闸门包含三件事：后端全量回归 → 评测契约测试 → 前端生产构建。

按需运行的专项验证：

```bash
# 全量评测
.venv/Scripts/python -m eval_refund.runner --limit 0

# 启用 LLM Judge
.venv/Scripts/python -m eval_refund.runner --limit 0 --judge

# 红蓝对抗
python backend/scripts/red_blue_test.py

# 语音压测
python backend/scripts/voice_load_test.py
```

---

<a id="sec-redlines"></a>

## 八、安全红线

以下为项目级硬性约束（完整 13 条见 [`AGENTS.md`](AGENTS.md)）：

1. 禁止调用真实退款渠道；评测、语音、陪练只能走 Mock / EVAL 隔离适配器。
2. 禁止修改真实密钥、绕过 JWT、RBAC、HITL 与工具安全网关。
3. 禁止删除、篡改或跳过既有测试来制造通过结果。
4. 订单/商品/价格核验为 `MISMATCH` / `UNKNOWN` 时必须挂起转人工，禁止自动退款。
5. 挂起前必须保存 `langgraph_thread_id` 检查点；恢复后不得重复执行退款等外部副作用。
6. 异常不得静默吞掉，必须写入 Trace / 降级队列并向上层反馈。
7. 安全服务故障时，资金副作用必须 Fail-Closed。

---

<a id="sec-docs"></a>

## 九、文档索引

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 项目定位、目录职责、安全红线与协作规则 |
| [`backend/README.md`](backend/README.md) | 后端环境、迁移与服务启动 |
| [`frontend/README.md`](frontend/README.md) | 前端工作台模块与开发约定 |


---

<a id="sec-status"></a>

## 十、当前状态与未决项

| 状态 | 内容 |
|---|---|
| 已实现（留有自测证据） | 客诉受理与订单核验链路、NER/DLP 脱敏、工具安全网关、多级意图路由、评测框架与 A/B 实验、版本与发布治理 |
| `PENDING` / 未实测 | 可信运行积攒、真实意图语料指标、生产订单与真实支付渠道对接 |

上述 `PENDING` 项属于部署与真实数据阶段，需取得真实环境证据后方可给出结论。本仓库为公开仓库，任何指标在获得实测证据前均不得表述为「已达标」。
