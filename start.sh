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

HOST="${CAIRN_HOST:-127.0.0.1}"
PORT="${CAIRN_PORT:-8888}"
CONFIG="${CAIRN_CONFIG:-$SCRIPT_DIR/dispatch.yaml}"

mkdir -p "$PID_DIR" "$LOG_DIR"

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

stop_all() {
    echo "[xrayAI] Stopping..."
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
    if [ -f "$PID_DIR/server.pid" ] && kill -0 "$(cat "$PID_DIR/server.pid")" 2>/dev/null; then
        echo "  server:     running (PID $(cat "$PID_DIR/server.pid"))"
    else
        echo "  server:     stopped"
    fi
    if [ -f "$PID_DIR/dispatcher.pid" ] && kill -0 "$(cat "$PID_DIR/dispatcher.pid")" 2>/dev/null; then
        echo "  dispatcher: running (PID $(cat "$PID_DIR/dispatcher.pid"))"
    else
        echo "  dispatcher: stopped"
    fi
}

start_all() {
    # Check if already running
    if [ -f "$PID_DIR/server.pid" ] && kill -0 "$(cat "$PID_DIR/server.pid")" 2>/dev/null; then
        echo "[xrayAI] Server is already running on http://$HOST:$PORT"
    else
        echo "[xrayAI] Starting Cairn server on $HOST:$PORT..."
        nohup uv run --project "$CAIRN_DIR" axiom serve \
            --host "$HOST" \
            --port "$PORT" \
            --no-access-log \
            --log-level info \
            > "$LOG_DIR/server.log" 2>&1 &
        SERVER_PID=$!
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

    if [ -f "$PID_DIR/dispatcher.pid" ] && kill -0 "$(cat "$PID_DIR/dispatcher.pid")" 2>/dev/null; then
        echo "[xrayAI] Dispatcher is already running."
    else
        echo "[xrayAI] Starting dispatcher (config=$CONFIG)..."
        nohup uv run --project "$CAIRN_DIR" axiom dispatch \
            --config "$CONFIG" \
            --log-level info \
            > "$LOG_DIR/dispatcher.log" 2>&1 &
        DISPATCHER_PID=$!
        echo "$DISPATCHER_PID" > "$PID_DIR/dispatcher.pid"
        sleep 2
        if kill -0 "$DISPATCHER_PID" 2>/dev/null; then
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
