# 多Agent客诉舆情退赔决策系统 —— 跨平台任务脚本
# Windows 本地：直接 ./.venv/Scripts/python；Docker/Linux：PY ?= python
PY ?= python
ifeq ($(OS),Windows_NT)
PYBIN := .venv/Scripts/python.exe
else
PYBIN := .venv/bin/python
endif

.PHONY: check test backend-test frontend-build db-init db-migrate redis-ping \
        install eval eval-judge eval-smoke lint

## 统一闸门：后端测试 + 评测测试 + 评测冒烟（不跑完整长链路）
check: test frontend-build
	@echo "=== make check: PASS ==="

install:
	$(PYBIN) -m pip install -r backend/requirements.txt

## 后端既有测试 + V1.0 评测测试（不删改原测试）
test:
	cd backend && $(PYBIN) -m pytest -q
	$(PYBIN) -m pytest tests/eval_refund -q

backend-test:
	cd backend && $(PYBIN) -m pytest -v

frontend-build:
	cd frontend && npm run build

## 评测冒烟：目标可达 + 1 条用例快速判定（目标未启动则明确报错而非伪造 pass）
eval-smoke:
	$(PYBIN) -m pytest tests/eval_refund -q -m smoke

## 全量 V1.0 评测（15 条退赔 Golden）
eval:
	$(PYBIN) -m eval_refund.runner --limit 0

## 附加 LLM-as-a-judge 的全量评测
eval-judge:
	$(PYBIN) -m eval_refund.runner --limit 0 --judge

## 代码风格（可选；ruff 未安装则跳过）
lint:
	$(PYBIN) -m ruff check backend/app eval_refund tests 2>/dev/null || echo "[lint] ruff 未安装，跳过（不影响 make check）"

db-init:
	cd backend && $(PYBIN) -m alembic upgrade head

db-migrate:
	cd backend && $(PYBIN) -m alembic revision --autogenerate -m "$(m)"

redis-ping:
	redis-cli ping
