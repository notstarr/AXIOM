#!/bin/bash
# Start the shared AXIOM mitmproxy with bounded, rotated JSONL logging.

set -u

MITM_PORT="${MITM_PORT:-18080}"
FLOW_FILE="${PROXY_FLOW_FILE:-/tmp/proxy_flows.jsonl}"
FLOW_MAX_BYTES="${PROXY_FLOW_MAX_BYTES:-67108864}"
FLOW_BACKUPS="${PROXY_FLOW_BACKUPS:-3}"
TARGET_HOST="${MITM_SHARED_TARGET_HOST:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON="${SCRIPT_DIR}/mitm_addon.py"
MITMDUMP="${MITMDUMP_BIN:-$(command -v mitmdump 2>/dev/null || true)}"
PYTHON="${PYTHON_BIN:-python3}"
PID_FILE="/tmp/axiom-mitmdump-${MITM_PORT}.pid"
LOCK_DIR="/tmp/axiom-mitmdump-${MITM_PORT}.lock"

_port_alive() {
    "${PYTHON}" - "${MITM_PORT}" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.3):
    pass
PY
}

_is_expected_pid() {
    local pid="$1"
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        return 1
    fi
    local cmd
    cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    [[ "${cmd}" == *"${ADDON}"* ]] || return 1
    [[ "${cmd}" == *"--listen-port ${MITM_PORT}"* ]] || return 1
    return 0
}

_find_expected_pid() {
    local pid cmd
    while read -r pid cmd; do
        [ -n "${pid}" ] || continue
        [[ "${cmd}" == *"${ADDON}"* ]] || continue
        [[ "${cmd}" == *"--listen-port ${MITM_PORT}"* ]] || continue
        echo "${pid}"
        return 0
    done < <(ps ax -o pid= -o command=)
    return 1
}

if [ -z "${MITMDUMP}" ] || [ ! -x "${MITMDUMP}" ]; then
    echo "❌ mitmdump 不可用；请检查 browser-control Skill Doctor。"
    exit 1
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    for _ in $(seq 1 20); do
        sleep 0.1
        if [ ! -d "${LOCK_DIR}" ]; then
            exec "$0" "$@"
        fi
    done
    echo "❌ mitmproxy 启动锁等待超时: ${LOCK_DIR}"
    exit 1
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

if [ -f "${PID_FILE}" ]; then
    existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if _is_expected_pid "${existing_pid}" && _port_alive; then
        echo "✅ AXIOM mitmproxy already running on port ${MITM_PORT} (pid=${existing_pid})"
        exit 0
    fi
    rm -f "${PID_FILE}"
fi

if _port_alive; then
    if pid="$(_find_expected_pid)"; then
        echo "${pid}" > "${PID_FILE}"
        echo "✅ AXIOM mitmproxy already running on port ${MITM_PORT} (pid=${pid})"
        exit 0
    fi
    echo "❌ Port ${MITM_PORT} is occupied by a non-AXIOM proxy process."
    exit 2
fi

new_pid="$(
    "${PYTHON}" - \
      "${MITMDUMP}" "${ADDON}" "${MITM_PORT}" "${FLOW_FILE}" \
      "${FLOW_MAX_BYTES}" "${FLOW_BACKUPS}" "${TARGET_HOST}" <<'PY'
import os
import subprocess
import sys

mitmdump, addon, port, flow_file, max_bytes, backups, target_host = sys.argv[1:]
allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
environment = {key: os.environ[key] for key in allowed if key in os.environ}
environment.update(
    {
        "PROXY_FLOW_FILE": flow_file,
        "PROXY_FLOW_MAX_BYTES": max_bytes,
        "PROXY_FLOW_BACKUPS": backups,
        "MITM_TARGET_HOST": target_host,
    }
)
process = subprocess.Popen(
    [mitmdump, "-s", addon, "--listen-port", port, "-q"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
    env=environment,
)
print(process.pid)
PY
)" || exit 1

case "${new_pid}" in
    ''|*[!0-9]*)
        echo "❌ mitmproxy launcher returned invalid pid: ${new_pid}"
        exit 1
        ;;
esac

for i in $(seq 1 10); do
    sleep 0.5
    if _port_alive; then
        ready_pid="$(_find_expected_pid || true)"
        if [ -n "${ready_pid}" ]; then
            echo "${ready_pid}" > "${PID_FILE}"
            echo "✅ AXIOM mitmproxy ready (${i}/2s, pid=${ready_pid}, flow=${FLOW_FILE})"
            exit 0
        fi
    fi
done

echo "❌ AXIOM mitmproxy failed to start"
if _is_expected_pid "${new_pid}"; then
    kill -TERM "${new_pid}" 2>/dev/null || true
fi
rm -f "${PID_FILE}"
exit 1
