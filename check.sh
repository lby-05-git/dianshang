#!/usr/bin/env bash
# 多Agent客诉舆情退赔决策系统 —— make check 的 bash 等价闸门（Git Bash / Linux / mac 用）
set -uo pipefail
cd "$(dirname "$0")"

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[check] 未找到虚拟环境，请先: python -m venv .venv"
  exit 1
fi

echo "===== [1/3] 后端全量回归测试 ====="
(cd backend && ../"$PY" -m pytest -q) || { echo "[check] 后端测试失败"; exit 1; }

echo ""
echo "===== [2/3] 评测契约测试 ====="
if [ -d tests/eval_refund ]; then
  "$PY" -m pytest tests/eval_refund -q || { echo "[check] 评测测试失败"; exit 1; }
else
  echo "[check] tests/eval_refund 不存在"
  exit 1
fi

echo ""
echo "===== [3/3] 前端生产构建 ====="
(cd frontend && npm run build) || { echo "[check] 前端构建失败"; exit 1; }

echo ""
echo "[check] ALL PASS"
