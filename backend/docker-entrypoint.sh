#!/bin/sh
# 容器入口：等待 MySQL → alembic 迁移（幂等）→ exec 实际命令
set -e

echo "[entrypoint] waiting for MySQL $DB_HOST:$DB_PORT ..."
until python - <<'EOF'
import os, socket, sys
host, port = os.environ["DB_HOST"], int(os.environ["DB_PORT"])
s = socket.socket(); s.settimeout(3)
try:
    s.connect((host, port))
except OSError:
    sys.exit(1)
finally:
    s.close()
EOF
do
  sleep 2
done
echo "[entrypoint] MySQL ready, running migrations ..."
alembic upgrade head
echo "[entrypoint] migrations done, starting: $@"
exec "$@"
