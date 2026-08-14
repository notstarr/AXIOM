#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./start.sh [start|stop|status]

  start   start PostgreSQL, migrate schema, then start AXIOM API and Runner
  stop    stop AXIOM API and Runner (PostgreSQL remains online)
  status  show API, Runner and PostgreSQL status
EOF
  exit 2
}

ACTION="${1:-start}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AXIOM_DIR="${AXIOM_CODE_DIR:-$SCRIPT_DIR/axiom}"
# shellcheck disable=SC1091
source "$AXIOM_DIR/ops/postgres/common.sh"
load_axiom_postgres_env

HOST="${AXIOM_HOST:-127.0.0.1}"
PORT="${AXIOM_PORT:-8888}"
API_URL="http://$HOST:$PORT"
PID_DIR="$AXIOM_DATA_DIR/run"
LOG_DIR="$AXIOM_DATA_DIR/logs"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
SERVER_LABEL="com.xrayai.axiom"
RUNNER_LABEL="com.xrayai.axiom.runner"
SERVER_PLIST="$LAUNCH_AGENT_DIR/$SERVER_LABEL.plist"
RUNNER_PLIST="$LAUNCH_AGENT_DIR/$RUNNER_LABEL.plist"
mkdir -p "$PID_DIR" "$LOG_DIR" "$LAUNCH_AGENT_DIR"

nmap_data_complete() {
  local directory="$1"
  [[ -f "$directory/nmap-services" \
    && -f "$directory/nmap-protocols" \
    && -f "$directory/nse_main.lua" \
    && -f "$directory/scripts/script.db" ]]
}

# The local Nmap binary is also reachable through /usr/local/bin/nmap, but
# that symlink makes Nmap derive its data path from /usr/local rather than
# ~/.local.  Export the validated data directory explicitly for both AXIOM
# workers and direct CLI use.
if [[ -n "${NMAPDIR:-}" ]]; then
  # Preserve an explicit operator override, including NMAPDIR=.
  NMAPDIR="$NMAPDIR"
else
  NMAPDIR="${AXIOM_NMAP_DATA_DIR:-$HOME/.local/share/nmap}"
  if ! nmap_data_complete "$NMAPDIR"; then
    NMAPDIR=""
  fi
fi

require_runtime() {
  [[ -x "$AXIOM_BIN" ]] || { echo "[axiom] runtime not found: $AXIOM_BIN" >&2; exit 1; }
  require_command docker
  require_command curl
}

use_launchctl() {
  [[ "$(uname -s)" == "Darwin" ]] \
    && command -v launchctl >/dev/null 2>&1 \
    && [[ "${XRAYAI_NO_LAUNCHCTL:-}" != "1" ]]
}

launch_domain() { printf 'gui/%s' "$(id -u)"; }
launch_pid() {
  launchctl print "$(launch_domain)/$1" 2>/dev/null | awk -F'= ' '/pid = / {print $2; exit}'
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
  python3 - "$plist" "$label" "$AXIOM_DIR" "$log_path" "$AXIOM_DATA_DIR" \
    "$AXIOM_PG_ENV_FILE" "$NMAPDIR" "$@" <<'PY'
import os, plistlib, sys
plist,label,workdir,log_path,data_dir,env_file,nmap_dir,*argv=sys.argv[1:]
payload={
    'Label': label,
    'ProgramArguments': argv,
    'WorkingDirectory': workdir,
    'StandardOutPath': log_path,
    'StandardErrorPath': log_path,
    'RunAtLoad': True,
    'KeepAlive': True,
    'EnvironmentVariables': {
        'AXIOM_DATA_DIR': data_dir,
        'AXIOM_PG_ENV_FILE': env_file,
        'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    },
}
if nmap_dir:
    payload['EnvironmentVariables']['NMAPDIR'] = nmap_dir
with open(plist,'wb') as handle: plistlib.dump(payload,handle)
os.chmod(plist, 0o600)
PY
}

health_is_current() {
  curl --connect-timeout 1 --max-time 2 -fsS "$API_URL/health" 2>/dev/null \
    | grep -q '"backend":"postgresql"'
}
wait_for_api() {
  echo -n "[axiom] 等待 PostgreSQL API 就绪"
  for _ in $(seq 1 90); do
    if health_is_current; then echo " ✓"; return 0; fi
    echo -n "."
    sleep 1
  done
  echo " 超时 ✗" >&2
  return 1
}

stop_pid_file() {
  local path="$1" label="$2"
  [[ -f "$path" ]] || return 0
  local pid
  pid="$(cat "$path")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL "$pid" 2>/dev/null || true
    echo "[axiom] $label stopped (PID $pid)"
  fi
  : > "$path"
}

stop_runtime() {
  if use_launchctl; then
    stop_launch_agent "$RUNNER_PLIST"
    stop_launch_agent "$SERVER_PLIST"
  fi
  stop_pid_file "$PID_DIR/runner.pid" "Runner"
  stop_pid_file "$PID_DIR/server.pid" "API"
}

start_runtime() {
  if use_launchctl; then
    write_launch_agent "$SERVER_PLIST" "$SERVER_LABEL" "$LOG_DIR/server.log" \
      "$AXIOM_BIN" serve --host "$HOST" --port "$PORT" --data-dir "$AXIOM_DATA_DIR" \
      --log-level info
    write_launch_agent "$RUNNER_PLIST" "$RUNNER_LABEL" "$LOG_DIR/runner.log" \
      "$AXIOM_BIN" runner --api-url "$API_URL"
    start_launch_agent "$SERVER_PLIST" "$SERVER_LABEL"
    wait_for_api
    start_launch_agent "$RUNNER_PLIST" "$RUNNER_LABEL"
    launch_pid "$SERVER_LABEL" > "$PID_DIR/server.pid"
    launch_pid "$RUNNER_LABEL" > "$PID_DIR/runner.pid"
  else
    if [[ -n "$NMAPDIR" ]]; then
      export NMAPDIR
    fi
    AXIOM_DATABASE_URL="$AXIOM_DATABASE_URL" nohup "$AXIOM_BIN" serve \
      --host "$HOST" --port "$PORT" --data-dir "$AXIOM_DATA_DIR" --log-level info \
      >"$LOG_DIR/server.log" 2>&1 &
    printf '%s\n' "$!" > "$PID_DIR/server.pid"
    wait_for_api
    nohup "$AXIOM_BIN" runner --api-url "$API_URL" >"$LOG_DIR/runner.log" 2>&1 &
    printf '%s\n' "$!" > "$PID_DIR/runner.pid"
  fi
}

status_all() {
  echo "[axiom] Status"
  if health_is_current; then echo "  API:        PostgreSQL / healthy"; else echo "  API:        unavailable"; fi
  if use_launchctl && launch_running "$RUNNER_LABEL"; then
    echo "  Runner:     running (PID $(launch_pid "$RUNNER_LABEL"))"
  elif [[ -s "$PID_DIR/runner.pid" ]] && kill -0 "$(cat "$PID_DIR/runner.pid")" 2>/dev/null; then
    echo "  Runner:     running (PID $(cat "$PID_DIR/runner.pid"))"
  else
    echo "  Runner:     stopped"
  fi
  if axiom_compose ps --status running postgres --format json 2>/dev/null | grep -q .; then
    echo "  PostgreSQL: running at $AXIOM_DB_HOST:$AXIOM_DB_PORT/$AXIOM_DB_NAME"
  else
    echo "  PostgreSQL: stopped"
  fi
  echo "  Data:       $AXIOM_DATA_DIR"
  echo "  Env:        $AXIOM_PG_ENV_FILE"
}

start_all() {
  sync_axiom_runtime
  require_runtime
  axiom_compose up -d postgres
  wait_for_postgres
  "$AXIOM_BIN" db upgrade --database-url "$AXIOM_MIGRATOR_DATABASE_URL" >/dev/null
  "$AXIOM_BIN" maintenance set normal --data-dir "$AXIOM_DATA_DIR" \
    --actor system.start --reason "PostgreSQL runtime start" >/dev/null
  stop_runtime
  start_runtime
  echo "[axiom] AXIOM 已启动：$API_URL"
  status_all
}

case "$ACTION" in
  start) start_all ;;
  stop) stop_runtime ;;
  status) status_all ;;
  *) usage ;;
esac
