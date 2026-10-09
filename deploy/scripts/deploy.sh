#!/usr/bin/env bash
# ClauseGuard一键部署/升级脚本（Linux）
# 用法:
#   bash deploy.sh install   # 首次部署：拷贝.env、构建并启动
#   bash deploy.sh upgrade   # 升级：重新构建app镜像并滚动替换（db数据卷不动）
#   bash deploy.sh rollback  # 回滚到上一镜像tag（需保留旧镜像）
#   bash deploy.sh stop|start|status
set -euo pipefail
cd "$(dirname "$0")/.."
CMD="${1:-status}"

case "$CMD" in
  install)
    [ -f .env ] || { cp .env.example .env; echo "已生成.env，请按需修改后重新执行"; }
    docker compose up -d db
    echo "等待数据库就绪..."
    until docker exec clauseguard-db pg_isready -U clauseguard >/dev/null 2>&1; do sleep 2; done
    docker compose up -d app
    echo "部署完成，健康检查:"; bash scripts/health_check.sh || true
    ;;
  upgrade)
    echo "== 升级前备份 =="
    bash scripts/backup.sh
    docker compose build app
    docker compose up -d app
    echo "升级完成，健康检查:"; bash scripts/health_check.sh || true
    ;;
  rollback)
    PREV=$(docker images clauseguard-app --format '{{.Tag}}' | sed -n '2p')
    [ -n "$PREV" ] || { echo "无可回滚的旧镜像"; exit 1; }
    docker tag "clauseguard-app:$PREV" clauseguard-app:current
    docker compose up -d app
    echo "已回滚到 $PREV"
    ;;
  stop)   docker compose stop ;;
  start)  docker compose start ;;
  status) docker compose ps; bash scripts/health_check.sh || true ;;
  *) echo "用法: deploy.sh install|upgrade|rollback|stop|start|status"; exit 1;;
esac
