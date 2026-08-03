#!/bin/bash
# Stop only the managed AXIOM mitmproxy instance for one listen port.

set -u

MITM_PORT="${MITM_PORT:-18080}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON="${SCRIPT_DIR}/mitm_addon.py"
PID_FILE="/tmp/axiom-mitmdump-${MITM_PORT}.pid"

_is_expected_pid() {
    local pid="$1"
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        return 1
    fi
    local cmd
    cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    [[ "${cmd}" == *"${ADDON}"* ]] || return 1
    [[ "${cmd}" == *"--listen-port ${MITM_PORT}"* ]] || return 1
}

target_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
if ! _is_expected_pid "${target_pid}"; then
    target_pid=""
    while read -r pid cmd; do
        [[ "${cmd}" == *"${ADDON}"* ]] || continue
        [[ "${cmd}" == *"--listen-port ${MITM_PORT}"* ]] || continue
        target_pid="${pid}"
        break
    done < <(ps ax -o pid= -o command=)
fi

if [ -z "${target_pid}" ]; then
    rm -f "${PID_FILE}"
    echo "ℹ️ AXIOM mitmproxy not running on port ${MITM_PORT}"
    exit 0
fi

target_pgid="$(ps -p "${target_pid}" -o pgid= 2>/dev/null | tr -d ' ' || true)"
if [ -z "${target_pgid}" ]; then
    target_pgid="${target_pid}"
fi
kill -TERM "-${target_pgid}" 2>/dev/null || true
for _ in $(seq 1 20); do
    if ! kill -0 "-${target_pgid}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        echo "✅ AXIOM mitmproxy stopped (pid=${target_pid})"
        exit 0
    fi
    sleep 0.25
done
kill -KILL "-${target_pgid}" 2>/dev/null || true
rm -f "${PID_FILE}"
echo "✅ AXIOM mitmproxy force stopped (pid=${target_pid})"
