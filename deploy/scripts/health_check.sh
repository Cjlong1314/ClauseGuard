#!/usr/bin/env bash
# ClauseGuard健康检查三探针：应用/数据库/LLM服务
# 用法: bash health_check.sh [应用URL，默认http://127.0.0.1:8600]
# 退出码: 0全部ok; 1有degraded(降级可用); 2有组件down(不可用)
set -u
APP_URL="${1:-http://127.0.0.1:8600}"
rc=2

echo "== ClauseGuard健康检查 $(date '+%F %T') =="

# 探针1: 应用自身（/health聚合db/llm/embedding降级可报）
if ! body=$(curl -fsS --max-time 8 "$APP_URL/health" 2>/dev/null); then
  echo "[应用] DOWN 无法连接 $APP_URL/health"
  echo "[数据库] UNKNOWN 应用不可达，改用直连探针..."
  # 应用挂了仍可直接探数据库端口兜底
  if command -v pg_isready >/dev/null 2>&1; then
    pg_isready -h 127.0.0.1 -p 5432 && rc=1 || rc=2
  fi
  echo "结论: 不健康"
  exit $rc
fi
echo "$body" | python3 -c "import sys,json;d=json.load(sys.stdin);print('[应用] /health整体:',d['status']);[print('[%s] %s %s'%(c['component'].upper(),c['status'],c.get('message',''))) for c in d['checks']]"
overall=$(echo "$body" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status']);print(all(c['status']!='degraded' for c in d['checks']))" | tail -1)
db_ok=$(echo "$body" | python3 -c "import sys,json;d=json.load(sys.stdin);print(all(c['component']!='database' or c['status']=='ok' for c in d['checks']))")
if [ "$db_ok" = "True" ]; then rc=0; else rc=2; fi

# 探针2: 数据库独立直连（不依赖应用）
if command -v pg_isready >/dev/null 2>&1; then
  pg_isready -h 127.0.0.1 -p "${PGPORT:-5432}" >/dev/null 2>&1 \
    && echo "[数据库] pg_isready OK" \
    || { echo "[数据库] pg_isready 失败"; rc=2; }
else
  echo "[数据库] pg_isready不可用，以/health中的database探针为准"
fi

# 探针3: LLM服务（可选，未配置视为降级而非故障）
llm_url=$(echo "$body" | python3 -c "import sys,json;d=json.load(sys.stdin);c=[x for x in d['checks'] if x['component']=='llm'][0];print(c.get('base_url',''))" 2>/dev/null)
if [ -n "$llm_url" ]; then
  if curl -fsS --max-time 8 "$llm_url/models" >/dev/null 2>&1; then
    echo "[LLM服务] $llm_url 可达"
  else
    echo "[LLM服务] $llm_url 不可达（AI研判将降级为规则引擎）"
    [ $rc -eq 0 ] && rc=1
  fi
else
  echo "[LLM服务] 未配置（degraded，规则引擎基线兜底）"
  [ $rc -eq 0 ] && rc=1
fi

case $rc in
  0) echo "结论: 健康";;
  1) echo "结论: 降级可用";;
  2) echo "结论: 不健康";;
esac
exit $rc
