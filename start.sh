#!/usr/bin/env bash
set -e

usage() {
    echo "Usage: $0 [start|stop|status]"
    exit 1
}

ACTION="${1:-start}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CAIRN_DIR="$SCRIPT_DIR/cairn"
PID_DIR="$CAIRN_DIR/.pids"
LOG_DIR="$CAIRN_DIR/logs"
MASTER_KEY_FILE="$HOME/.config/cairn/master.key"
MOZZIE_TOKEN_FILE="$HOME/.config/cairn/mozzie.token"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
SERVER_LABEL="com.xrayai.cairn.server"
DISPATCHER_LABEL="com.xrayai.cairn.dispatcher"
LEGACY_DISPATCHER_LABEL="com.axiom.dispatcher"
SERVER_PLIST="$LAUNCH_AGENT_DIR/$SERVER_LABEL.plist"
DISPATCHER_PLIST="$LAUNCH_AGENT_DIR/$DISPATCHER_LABEL.plist"
LEGACY_DISPATCHER_PLIST="$LAUNCH_AGENT_DIR/$LEGACY_DISPATCHER_LABEL.plist"

HOST="${CAIRN_HOST:-127.0.0.1}"
PORT="${CAIRN_PORT:-8888}"
CONFIG="${CAIRN_CONFIG:-$SCRIPT_DIR/dispatch.yaml}"

mkdir -p "$PID_DIR" "$LOG_DIR"

use_launchctl() {
    [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1 && [ "${XRAYAI_NO_LAUNCHCTL:-}" != "1" ]
}

launch_domain() {
    echo "gui/$(id -u)"
}

launch_pid() {
    local label="$1"
    launchctl print "$(launch_domain)/$label" 2>/dev/null | awk -F'= ' '/pid = / {print $2; exit}'
}

launch_running() {
    local label="$1"
    local pid
    pid="$(launch_pid "$label")"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

server_pid() {
    if use_launchctl && launch_running "$SERVER_LABEL"; then
        launch_pid "$SERVER_LABEL"
        return
    fi
    if [ -f "$PID_DIR/server.pid" ] && kill -0 "$(cat "$PID_DIR/server.pid")" 2>/dev/null; then
        cat "$PID_DIR/server.pid"
        return
    fi
    pgrep -f "$CAIRN_DIR/.venv/bin/axiom serve.*--port $PORT" | head -1 || true
}

dispatcher_pid() {
    if use_launchctl && launch_running "$DISPATCHER_LABEL"; then
        launch_pid "$DISPATCHER_LABEL"
        return
    fi
    if use_launchctl && launch_running "$LEGACY_DISPATCHER_LABEL"; then
        launch_pid "$LEGACY_DISPATCHER_LABEL"
        return
    fi
    if [ -f "$PID_DIR/dispatcher.pid" ] && kill -0 "$(cat "$PID_DIR/dispatcher.pid")" 2>/dev/null; then
        cat "$PID_DIR/dispatcher.pid"
        return
    fi
    pgrep -f "$CAIRN_DIR/.venv/bin/axiom dispatch.*--config" | head -1 || true
}

write_launch_agent() {
    local plist="$1"
    local label="$2"
    local workdir="$3"
    local log_path="$4"
    shift 4
    mkdir -p "$LAUNCH_AGENT_DIR"
    python3 - "$plist" "$label" "$workdir" "$log_path" "$MASTER_KEY" "${MOZZIE_TOKEN:-}" "$@" <<'PY'
import plistlib
import sys

plist, label, workdir, log_path, master_key, mozzie_token, *argv = sys.argv[1:]
env = {
    "MASTER_KEY": master_key,
    "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
}
if mozzie_token:
    env["MOZZIE_TOKEN"] = mozzie_token
payload = {
    "Label": label,
    "ProgramArguments": argv,
    "WorkingDirectory": workdir,
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
    "RunAtLoad": True,
    "KeepAlive": True,
    "EnvironmentVariables": env,
}
with open(plist, "wb") as fh:
    plistlib.dump(payload, fh)
PY
}

start_launch_agent() {
    local plist="$1"
    local label="$2"
    launchctl bootout "$(launch_domain)" "$plist" >/dev/null 2>&1 || true
    launchctl bootstrap "$(launch_domain)" "$plist"
    launchctl kickstart -k "$(launch_domain)/$label" >/dev/null 2>&1 || true
}

stop_launch_agent() {
    local plist="$1"
    launchctl bootout "$(launch_domain)" "$plist" >/dev/null 2>&1 || true
}

# Ensure MASTER_KEY is available for API key encryption/decryption.
if [ -n "${MASTER_KEY:-}" ]; then
    export MASTER_KEY
elif [ -f "$MASTER_KEY_FILE" ]; then
    export MASTER_KEY="$(cat "$MASTER_KEY_FILE")"
else
    # Generate and persist a random master key on first run.
    mkdir -p "$(dirname "$MASTER_KEY_FILE")"
    MASTER_KEY="$(python3 -c "import secrets; print(secrets.token_hex(32))")"
    echo "$MASTER_KEY" > "$MASTER_KEY_FILE"
    chmod 600 "$MASTER_KEY_FILE"
    export MASTER_KEY
    echo "[xrayAI] Generated MASTER_KEY at $MASTER_KEY_FILE"
fi

if [ -z "${MOZZIE_TOKEN:-}" ] && [ -f "$MOZZIE_TOKEN_FILE" ]; then
    export MOZZIE_TOKEN="$(cat "$MOZZIE_TOKEN_FILE")"
fi

stop_all() {
    echo "[xrayAI] Stopping..."
    if use_launchctl; then
        stop_launch_agent "$SERVER_PLIST"
        stop_launch_agent "$DISPATCHER_PLIST"
        stop_launch_agent "$LEGACY_DISPATCHER_PLIST"
    fi
    if [ -f "$PID_DIR/server.pid" ]; then
        kill "$(cat "$PID_DIR/server.pid")" 2>/dev/null && echo "  server stopped" || true
        rm -f "$PID_DIR/server.pid"
    fi
    if [ -f "$PID_DIR/dispatcher.pid" ]; then
        kill "$(cat "$PID_DIR/dispatcher.pid")" 2>/dev/null && echo "  dispatcher stopped" || true
        rm -f "$PID_DIR/dispatcher.pid"
    fi
    # Also kill any orphaned processes
    pkill -f "axiom serve" 2>/dev/null || true
    pkill -f "axiom dispatch" 2>/dev/null || true
    echo "[xrayAI] Done."
}

status_all() {
    echo "[xrayAI] Status:"
    local server_existing
    server_existing="$(server_pid)"
    if [ -n "$server_existing" ]; then
        echo "  server:     running (PID $server_existing)"
    else
        echo "  server:     stopped"
    fi
    local dispatcher_existing
    dispatcher_existing="$(dispatcher_pid)"
    if [ -n "$dispatcher_existing" ]; then
        echo "  dispatcher: running (PID $dispatcher_existing)"
    else
        echo "  dispatcher: stopped"
    fi
}

start_all() {
    # Check if already running
    SERVER_PID="$(server_pid)"
    if [ -n "$SERVER_PID" ]; then
        echo "$SERVER_PID" > "$PID_DIR/server.pid"
        echo "[xrayAI] Server is already running on http://$HOST:$PORT"
    else
        echo "[xrayAI] Starting Cairn server on $HOST:$PORT..."
        if use_launchctl; then
            write_launch_agent "$SERVER_PLIST" "$SERVER_LABEL" "$CAIRN_DIR" "$LOG_DIR/server.log" \
                /bin/bash -lc "cd '$CAIRN_DIR' && exec uv run --project '$CAIRN_DIR' axiom serve --host '$HOST' --port '$PORT' --no-access-log --log-level info"
            start_launch_agent "$SERVER_PLIST" "$SERVER_LABEL"
            sleep 1
            SERVER_PID="$(launch_pid "$SERVER_LABEL")"
        else
            nohup uv run --project "$CAIRN_DIR" axiom serve \
                --host "$HOST" \
                --port "$PORT" \
                --no-access-log \
                --log-level info \
                > "$LOG_DIR/server.log" 2>&1 &
            SERVER_PID=$!
            disown "$SERVER_PID"
        fi
        echo "$SERVER_PID" > "$PID_DIR/server.pid"

        # Wait for server to be ready
        echo -n "[xrayAI] Waiting for server"
        for i in $(seq 1 30); do
            if curl -s "http://$HOST:$PORT/projects" > /dev/null 2>&1; then
                echo " ready."
                break
            fi
            if ! kill -0 "$SERVER_PID" 2>/dev/null; then
                echo ""
                echo "[xrayAI] ERROR: Server failed to start. Check $LOG_DIR/server.log"
                cat "$LOG_DIR/server.log" | tail -20
                exit 1
            fi
            echo -n "."
            sleep 1
        done

        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo ""
            echo "[xrayAI] ERROR: Server process died."
            cat "$LOG_DIR/server.log" | tail -20
            exit 1
        fi
    fi

    DISPATCHER_PID="$(dispatcher_pid)"
    if [ -n "$DISPATCHER_PID" ]; then
        echo "$DISPATCHER_PID" > "$PID_DIR/dispatcher.pid"
        echo "[xrayAI] Dispatcher is already running."
    else
        echo "[xrayAI] Starting dispatcher (config=$CONFIG)..."
        if use_launchctl; then
            write_launch_agent "$DISPATCHER_PLIST" "$DISPATCHER_LABEL" "$CAIRN_DIR" "$LOG_DIR/dispatcher.log" \
                /bin/bash -lc "cd '$CAIRN_DIR' && exec uv run --project '$CAIRN_DIR' axiom dispatch --config '$CONFIG' --log-level info"
            start_launch_agent "$DISPATCHER_PLIST" "$DISPATCHER_LABEL"
            sleep 2
            DISPATCHER_PID="$(launch_pid "$DISPATCHER_LABEL")"
        else
            nohup uv run --project "$CAIRN_DIR" axiom dispatch \
                --config "$CONFIG" \
                --log-level info \
                > "$LOG_DIR/dispatcher.log" 2>&1 &
            DISPATCHER_PID=$!
            disown "$DISPATCHER_PID"
            sleep 2
        fi
        echo "$DISPATCHER_PID" > "$PID_DIR/dispatcher.pid"
        if [ -n "$DISPATCHER_PID" ] && kill -0 "$DISPATCHER_PID" 2>/dev/null; then
            echo "[xrayAI] Dispatcher started (PID $DISPATCHER_PID)."
        else
            echo "[xrayAI] ERROR: Dispatcher failed to start. Check $LOG_DIR/dispatcher.log"
            cat "$LOG_DIR/dispatcher.log" | tail -30
            exit 1
        fi
    fi

    echo ""
    echo "============================================"
    echo "  xrayAI is running"
    echo "  Server:     http://$HOST:$PORT"
    echo "  Logs:       $LOG_DIR/"
    echo "  Stop with:  $0 stop"
    echo "============================================"
}

case "$ACTION" in
    start)   start_all ;;
    stop)    stop_all ;;
    status)  status_all ;;
    *)       usage ;;
esac
