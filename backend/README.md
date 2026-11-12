# backend

多Agent客诉舆情退赔决策系统 · 后端（FastAPI）

## 环境

- Python 3.12（conda env: `D:\anaconda3\envs\ksyqtpjc`）
- MySQL 8.0（开发实例：`D:\MySQLData`，root 空密码，端口 3306）
- Redis（D:\Redis 服务，端口 6379）

## 常用命令（Windows）

```bash
# 启动后端（开发）
cd backend
D:\anaconda3\envs\ksyqtpjc\python.exe -m uvicorn app.main:app --reload --port 8000

# 跑测试
D:\anaconda3\envs\ksyqtpjc\python.exe -m pytest -v

# 启动 MySQL 开发实例（重启电脑后）
"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe" --datadir=D:\MySQLData --port=3306 --console
```

## 目录结构（规格 docs/06 WBS）

```
backend/
├─ app/
│  ├─ main.py            # 应用入口 + 健康检查
│  ├─ core/              # 配置 / 安全 / 工具
│  ├─ api/               # 路由（Phase 3）
│  ├─ domain/            # 领域模型与状态机（Phase 2）
│  ├─ agents/            # 四 Agent + 决策策略（Phase 5）
│  ├─ workflow/          # LangGraph 图 + Checkpointer（Phase 5）
│  ├─ infrastructure/    # DB / Redis / OCR / LLM / 退款适配器（Phase 2/4/6）
│  └─ worker/            # Stream 消费者（Phase 4）
└─ tests/                # pytest（含接口防重测试，Phase 7）
```
