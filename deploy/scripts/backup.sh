#!/usr/bin/env bash
# ClauseGuard PostgreSQL备份：每日全量+WAL归档说明
# 建议crontab: 30 2 * * * /opt/clauseguard/deploy/scripts/backup.sh >> /var/log/cg_backup.log 2>&1
#
# WAL归档（Point-in-Time Recovery）在数据库启动参数中开启：
#   postgresql.conf: archive_mode=on, archive_command='test ! -f /var/lib/postgresql/wal/%f && cp %p /var/lib/postgresql/wal/%f'
#   compose场景可在db服务挂载 ./wal:/var/lib/postgresql/wal 并通过自定义command传入
#   恢复时用 base backup + restore_command 重放到目标时间点（见deploy/README.md恢复演练节）
set -euo pipefail
cd "$(dirname "$0")/.."   # deploy/

source ./.env 2>/dev/null || true
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

# 优先用docker容器内pg_dump（无需宿主机装PG客户端）
if docker ps --format '{{.Names}}' | grep -q '^clauseguard-db$'; then
  echo "== 容器内全量备份 =="
  docker exec clauseguard-db pg_dump -U "${POSTGRES_USER:-clauseguard}" -d "${POSTGRES_DB:-clauseguard}" -Fc \
    > "$BACKUP_DIR/cg_full_$STAMP.dump"
else
  echo "== 宿主机pg_dump全量备份 =="
  pg_dump "${DATABASE_URL:-postgresql://clauseguard:Cg%402026pg@127.0.0.1:5432/clauseguard}" -Fc \
    -f "$BACKUP_DIR/cg_full_$STAMP.dump"
fi
echo "已生成: $BACKUP_DIR/cg_full_$STAMP.dump ($(du -h "$BACKUP_DIR/cg_full_$STAMP.dump" | cut -f1))"

# 滚动清理过期备份
find "$BACKUP_DIR" -name 'cg_full_*.dump' -mtime +"$KEEP_DAYS" -delete
echo "清理${KEEP_DAYS}天前备份完成"
