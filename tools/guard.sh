#!/usr/bin/env bash
# 容器守护脚本（任务三验收"服务容灾"补位方案）
# 背景：Docker restart policy 只对容器自身崩溃生效；docker kill 属于显式停止，
#       无论 always/unless-stopped 都不会自动拉起。本脚本检测 rfs-* 容器
#       不在运行即 docker start（3s 轮询，满足"5 秒内自动拉起"验收）。
# 用法：bash tools/guard.sh  （建议注册为系统服务/cron，或在 compose 启动后挂后台）
set -u

SERVICES=(rfs-mysql rfs-redis rfs-backend rfs-worker rfs-web)
DOCKER="${DOCKER:-docker}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

while true; do
  for c in "${SERVICES[@]}"; do
    running=$("$DOCKER" inspect -f '{{.State.Running}}' "$c" 2>/dev/null | tr -d '\r')
    if [ "$running" != "true" ]; then
      log "guard: $c not running, restarting..."
      "$DOCKER" start "$c" >/dev/null 2>&1 || log "guard: $c start failed"
    fi
  done
  sleep 3
done
