#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-all}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST="${AXIOM_HOST:-127.0.0.1}"
PORT="${AXIOM_PORT:-8888}"
DOMAIN="gui/$(id -u)"
SERVER_LABEL="com.xrayai.axiom"
RUNNER_LABEL="com.xrayai.axiom.runner"

wait_for_api() {
  echo -n "[axiom] 等待 API 就绪"
  for _ in $(seq 1 60); do
    if curl --connect-timeout 1 --max-time 2 -fsS "http://$HOST:$PORT/health" 2>/dev/null \
      | grep -q '"api":"v1"'; then
      echo " ✓"
      return 0
    fi
    echo -n "."
    sleep 1
  done
  echo " 超时 ✗" >&2
  return 1
}

restart_label() {
  local label="$1"
  if ! launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    echo "[axiom] $label 尚未安装，请先运行 ./start.sh start" >&2
    exit 1
  fi
  launchctl kickstart -k "$DOMAIN/$label"
}

case "$TARGET" in
  server)
    restart_label "$SERVER_LABEL"
    wait_for_api
    ;;
  runner)
    restart_label "$RUNNER_LABEL"
    ;;
  all)
    restart_label "$SERVER_LABEL"
    wait_for_api
    restart_label "$RUNNER_LABEL"
    ;;
  *)
    echo "用法: ./restart.sh [all|server|runner]" >&2
    exit 1
    ;;
esac

echo "[axiom] 重启完成：http://$HOST:$PORT"
