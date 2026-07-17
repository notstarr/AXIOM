#!/usr/bin/env bash
# 一键重启 axiom（前端 + 后端 + 调度器）
# 用法：
#   ./restart.sh            重启全部（server + dispatcher）
#   ./restart.sh server      仅重启前端 + 后端 API（8888）
#   ./restart.sh dispatcher  仅重启任务调度器
set -euo pipefail

DOMAIN="gui/$(id -u)"
HOST="${AXIOM_HOST:-127.0.0.1}"
PORT="${AXIOM_PORT:-8888}"
TARGET="${1:-all}"

restart() {
  local label="$1"
  echo "[axiom] kickstart $label ..."
  launchctl kickstart -k "$DOMAIN/$label"
}

case "$TARGET" in
  server)
    restart com.xrayai.cairn
    ;;
  dispatcher)
    restart com.xrayai.cairn.dispatcher
    ;;
  all)
    restart com.xrayai.cairn
    restart com.xrayai.cairn.dispatcher
    ;;
  *)
    echo "用法: $0 [all|server|dispatcher]" >&2
    exit 1
    ;;
esac

if [[ "$TARGET" != "dispatcher" ]]; then
  echo -n "[axiom] 等待 API 就绪"
  for i in $(seq 1 20); do
    if curl -s -o /dev/null "http://$HOST:$PORT/projects"; then
      echo " 就绪 ✓"
      break
    fi
    echo -n "."
    sleep 1
  done
fi

echo "[axiom] 完成。前端: http://$HOST:$PORT"
