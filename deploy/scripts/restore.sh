#!/usr/bin/env bash
# ClauseGuard恢复：从全量备份恢复PostgreSQL
# 用法: bash restore.sh <backup.dump路径>
# 警告: 恢复会覆盖目标库数据，执行前确认业务窗口并已再次备份
set -euo pipefail
DUMP="${1:?用法: restore.sh <backup.dump路径>}"
cd "$(dirname "$0")/.."
source ./.env 2>/dev/null || true

if docker ps --format '{{.Names}}' | grep -q '^clauseguard-db$'; then
  echo "== 停应用避免写入 =="
  docker stop clauseguard-app 2>/dev/null || true
  echo "== 恢复 $DUMP =="
  # drop+recreate保证干净恢复
  docker exec clauseguard-db psql -U "${POSTGRES_USER:-clauseguard}" -d postgres -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${POSTGRES_DB:-clauseguard}' AND pid<>pg_backend_pid();"
  docker exec clauseguard-db psql -U "${POSTGRES_USER:-clauseguard}" -d postgres -c "DROP DATABASE IF EXISTS \"${POSTGRES_DB:-clauseguard}\";"
  docker exec clauseguard-db psql -U "${POSTGRES_USER:-clauseguard}" -d postgres -c "CREATE DATABASE \"${POSTGRES_DB:-clauseguard}\";"
  docker exec -i clauseguard-db pg_restore -U "${POSTGRES_USER:-clauseguard}" -d "${POSTGRES_DB:-clauseguard}" < "$DUMP"
  echo "== 重启应用 =="
  docker start clauseguard-app
else
  echo "== 宿主机psql恢复 =="
  psql "${DATABASE_URL}" -c "DROP DATABASE IF EXISTS clauseguard;" -c "CREATE DATABASE clauseguard;"
  pg_restore -d "${DATABASE_URL/postgresql:\/\/[^\/]*\/clauseguard/postgresql:$DATABASE_URL}" "$DUMP" 2>/dev/null \
    || pg_restore --dbname="$DATABASE_URL" "$DUMP"
fi
echo "恢复完成，请执行健康检查: bash health_check.sh"
