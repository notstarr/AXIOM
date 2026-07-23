#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./start.sh [start|stop|status|rollback]

  start     stop the legacy runtime, migrate once when needed, then start AXIOM
  stop      stop the new AXIOM API and Runner
  status    show API/Runner process and health status
  rollback  stop the new runtime and restore the preserved legacy LaunchAgents
EOF
  exit 1
}

ACTION="${1:-start}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AXIOM_DIR="${AXIOM_CODE_DIR:-$SCRIPT_DIR/axiom}"

HOST="${AXIOM_HOST:-127.0.0.1}"
PORT="${AXIOM_PORT:-8888}"
API_URL="http://$HOST:$PORT"

# Keep the new runtime completely separate from the preserved Cairn runtime.
LEGACY_DATA_DIR="${AXIOM_LEGACY_DATA_DIR:-$HOME/.local/share/axiom}"
LEGACY_DB="${AXIOM_LEGACY_DB:-$LEGACY_DATA_DIR/axiom.db}"
AXIOM_DATA_DIR="${AXIOM_DATA_DIR:-$HOME/.local/share/axiom-v3}"
AXIOM_DB="${AXIOM_DB:-$AXIOM_DATA_DIR/axiom.db}"
PID_DIR="$AXIOM_DATA_DIR/run"
LOG_DIR="$AXIOM_DATA_DIR/logs"

LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
SERVER_LABEL="com.xrayai.axiom"
RUNNER_LABEL="com.xrayai.axiom.runner"
SERVER_PLIST="$LAUNCH_AGENT_DIR/$SERVER_LABEL.plist"
RUNNER_PLIST="$LAUNCH_AGENT_DIR/$RUNNER_LABEL.plist"

# These files are intentionally preserved for an immediate rollback.
LEGACY_SERVER_LABEL="com.xrayai.cairn"
LEGACY_RUNNER_LABEL="com.xrayai.cairn.dispatcher"
LEGACY_DISPATCHER_LABEL="com.axiom.dispatcher"
LEGACY_SERVER_PLIST="$LAUNCH_AGENT_DIR/$LEGACY_SERVER_LABEL.plist"
LEGACY_RUNNER_PLIST="$LAUNCH_AGENT_DIR/$LEGACY_RUNNER_LABEL.plist"
LEGACY_DISPATCHER_PLIST="$LAUNCH_AGENT_DIR/$LEGACY_DISPATCHER_LABEL.plist"

AXIOM_BIN="$AXIOM_DIR/.venv/bin/axiom"

mkdir -p "$PID_DIR" "$LOG_DIR" "$LAUNCH_AGENT_DIR"

require_runtime() {
  if [[ ! -x "$AXIOM_BIN" ]]; then
    echo "[axiom] ERROR: runtime not found: $AXIOM_BIN" >&2
    exit 1
  fi
}

use_launchctl() {
  [[ "$(uname -s)" == "Darwin" ]] \
    && command -v launchctl >/dev/null 2>&1 \
    && [[ "${XRAYAI_NO_LAUNCHCTL:-}" != "1" ]]
}

launch_domain() {
  printf 'gui/%s' "$(id -u)"
}

launch_pid() {
  local label="$1"
  launchctl print "$(launch_domain)/$label" 2>/dev/null \
    | awk -F'= ' '/pid = / {print $2; exit}'
}

launch_running() {
  local pid
  pid="$(launch_pid "$1")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

stop_launch_agent() {
  local plist="$1"
  [[ -f "$plist" ]] || return 0
  launchctl bootout "$(launch_domain)" "$plist" >/dev/null 2>&1 || true
}

start_launch_agent() {
  local plist="$1" label="$2"
  stop_launch_agent "$plist"
  launchctl bootstrap "$(launch_domain)" "$plist"
  launchctl kickstart -k "$(launch_domain)/$label" >/dev/null 2>&1 || true
}

write_launch_agent() {
  local plist="$1" label="$2" log_path="$3"
  shift 3
  python3 - "$plist" "$label" "$AXIOM_DIR" "$log_path" "$AXIOM_DATA_DIR" "$@" <<'PY'
import plistlib
import sys

plist, label, workdir, log_path, data_dir, *argv = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": argv,
    "WorkingDirectory": workdir,
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
    "RunAtLoad": True,
    "KeepAlive": True,
    "EnvironmentVariables": {
        "AXIOM_DATA_DIR": data_dir,
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
}
with open(plist, "wb") as handle:
    plistlib.dump(payload, handle)
PY
}

health_is_current() {
  curl --connect-timeout 1 --max-time 2 -fsS "$API_URL/health" 2>/dev/null \
    | grep -q '"api":"v1"'
}

health_is_legacy() {
  curl --connect-timeout 1 --max-time 2 -fsS "$API_URL/health" 2>/dev/null \
    | grep -q '"api":"v2"'
}

wait_for_health() {
  local mode="$1"
  echo -n "[axiom] 等待 $mode API 就绪"
  for _ in $(seq 1 60); do
    if [[ "$mode" == "new" ]] && health_is_current; then
      echo " ✓"
      return 0
    fi
    if [[ "$mode" == "legacy" ]] && health_is_legacy; then
      echo " ✓"
      return 0
    fi
    echo -n "."
    sleep 1
  done
  echo " 超时 ✗" >&2
  return 1
}

server_pid() {
  if use_launchctl && launch_running "$SERVER_LABEL"; then
    launch_pid "$SERVER_LABEL"
    return
  fi
  if [[ -f "$PID_DIR/server.pid" ]]; then
    local pid
    pid="$(cat "$PID_DIR/server.pid")"
    if kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return
    fi
  fi
  pgrep -f "$AXIOM_BIN serve.*--port $PORT" | head -1 || true
}

runner_pid() {
  if use_launchctl && launch_running "$RUNNER_LABEL"; then
    launch_pid "$RUNNER_LABEL"
    return
  fi
  if [[ -f "$PID_DIR/runner.pid" ]]; then
    local pid
    pid="$(cat "$PID_DIR/runner.pid")"
    if kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "$pid"
      return
    fi
  fi
  pgrep -f "$AXIOM_BIN runner.*--api-url $API_URL" | head -1 || true
}

stop_pid_file() {
  local path="$1" label="$2"
  [[ -f "$path" ]] || return 0
  local pid
  pid="$(cat "$path")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "$pid" 2>/dev/null || true
    echo "[axiom] $label stopped (PID $pid)"
  fi
  rm -f "$path"
}

stop_new_runtime() {
  if use_launchctl; then
    stop_launch_agent "$RUNNER_PLIST"
    stop_launch_agent "$SERVER_PLIST"
  fi
  stop_pid_file "$PID_DIR/runner.pid" "Runner"
  stop_pid_file "$PID_DIR/server.pid" "API"
}

stop_legacy_runtime() {
  if use_launchctl; then
    stop_launch_agent "$LEGACY_RUNNER_PLIST"
    stop_launch_agent "$LEGACY_DISPATCHER_PLIST"
    stop_launch_agent "$LEGACY_SERVER_PLIST"
  fi
  # Only stop preserved Cairn commands; never use a broad `pkill axiom`.
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done < <(pgrep -f '/Users/hexin/vscode/xrayAI/cairn/.venv/bin/axiom (serve|dispatch)' || true)

  # Do not snapshot the legacy database while a restarted legacy process can
  # still write its WAL.  This is also the hand-off barrier for migration.
  for _ in $(seq 1 50); do
    if ! pgrep -f '/Users/hexin/vscode/xrayAI/cairn/.venv/bin/axiom (serve|dispatch)' >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "[axiom] ERROR: legacy runtime did not stop before migration" >&2
  exit 1
}

prepare_database() {
  if [[ -s "$AXIOM_DB" ]]; then
    "$AXIOM_BIN" db-upgrade --db-path "$AXIOM_DB" >/dev/null
    return
  fi
  if [[ ! -s "$LEGACY_DB" ]]; then
    echo "[axiom] ERROR: legacy database not found: $LEGACY_DB" >&2
    exit 1
  fi

  local stamp backup_dir source_snapshot report_path
  stamp="$(date +%Y%m%d-%H%M%S)"
  backup_dir="$AXIOM_DATA_DIR/backups/$stamp"
  source_snapshot="$backup_dir/legacy-source.db"
  report_path="$backup_dir/migration-report.json"
  mkdir -p "$backup_dir"

  echo "[axiom] 创建一致性数据库快照：$source_snapshot"
  python3 - "$LEGACY_DB" "$source_snapshot" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
PY

  echo "[axiom] 迁移旧生产数据到独立的新运行目录"
  "$AXIOM_BIN" migrate-legacy \
    --source-db "$source_snapshot" \
    --target-db "$AXIOM_DB" \
    --source-data-dir "$LEGACY_DATA_DIR" \
    --target-data-dir "$AXIOM_DATA_DIR" \
    --report-path "$report_path" \
    >/dev/null
  echo "[axiom] 迁移报告：$report_path"
}

start_new_runtime() {
  if use_launchctl; then
    write_launch_agent "$SERVER_PLIST" "$SERVER_LABEL" "$LOG_DIR/server.log" \
      "$AXIOM_BIN" serve \
      --host "$HOST" --port "$PORT" \
      --db-path "$AXIOM_DB" --data-dir "$AXIOM_DATA_DIR" \
      --log-level info
    write_launch_agent "$RUNNER_PLIST" "$RUNNER_LABEL" "$LOG_DIR/runner.log" \
      "$AXIOM_BIN" runner --api-url "$API_URL"
    start_launch_agent "$SERVER_PLIST" "$SERVER_LABEL"
    wait_for_health new
    start_launch_agent "$RUNNER_PLIST" "$RUNNER_LABEL"
    printf '%s\n' "$(launch_pid "$SERVER_LABEL")" > "$PID_DIR/server.pid"
    printf '%s\n' "$(launch_pid "$RUNNER_LABEL")" > "$PID_DIR/runner.pid"
  else
    nohup "$AXIOM_BIN" serve \
      --host "$HOST" --port "$PORT" \
      --db-path "$AXIOM_DB" --data-dir "$AXIOM_DATA_DIR" \
      --log-level info >"$LOG_DIR/server.log" 2>&1 &
    printf '%s\n' "$!" > "$PID_DIR/server.pid"
    wait_for_health new
    nohup "$AXIOM_BIN" runner --api-url "$API_URL" \
      >"$LOG_DIR/runner.log" 2>&1 &
    printf '%s\n' "$!" > "$PID_DIR/runner.pid"
  fi
}

status_all() {
  local api_pid run_pid
  api_pid="$(server_pid)"
  run_pid="$(runner_pid)"
  echo "[axiom] Status"
  if [[ -n "$api_pid" ]]; then
    echo "  API:      running (PID $api_pid)"
  else
    echo "  API:      stopped"
  fi
  if [[ -n "$run_pid" ]]; then
    echo "  Runner:   running (PID $run_pid)"
  else
    echo "  Runner:   stopped"
  fi
  if health_is_current; then
    echo "  Health:   current"
  elif health_is_legacy; then
    echo "  Health:   legacy"
  else
    echo "  Health:   unavailable"
  fi
  echo "  Database: $AXIOM_DB"
  echo "  Data:     $AXIOM_DATA_DIR"
}

start_all() {
  require_runtime
  stop_new_runtime
  stop_legacy_runtime
  prepare_database
  start_new_runtime
  echo "[axiom] AXIOM 已启动：$API_URL"
  status_all
}

rollback_legacy() {
  stop_new_runtime
  if ! use_launchctl; then
    echo "[axiom] ERROR: rollback currently requires the preserved macOS LaunchAgents" >&2
    exit 1
  fi
  [[ -f "$LEGACY_SERVER_PLIST" ]] || {
    echo "[axiom] ERROR: legacy server plist is missing" >&2
    exit 1
  }
  start_launch_agent "$LEGACY_SERVER_PLIST" "$LEGACY_SERVER_LABEL"
  wait_for_health legacy
  if [[ -f "$LEGACY_RUNNER_PLIST" ]]; then
    start_launch_agent "$LEGACY_RUNNER_PLIST" "$LEGACY_RUNNER_LABEL"
  elif [[ -f "$LEGACY_DISPATCHER_PLIST" ]]; then
    start_launch_agent "$LEGACY_DISPATCHER_PLIST" "$LEGACY_DISPATCHER_LABEL"
  fi
  echo "[axiom] 已回滚到保留的旧运行时：$API_URL"
}

case "$ACTION" in
  start) start_all ;;
  stop) stop_new_runtime ;;
  status) status_all ;;
  rollback) rollback_legacy ;;
  *) usage ;;
esac
