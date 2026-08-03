#!/bin/bash
# Stop only Chrome-SecTest (safe shutdown).
# Never touch user's personal Chrome process.
#
# Matching rules:
# 1) Prefer PID in /tmp/chrome-sectest-<port>.pid
# 2) Verify process command contains BOTH:
#    --remote-debugging-port=<port>
#    --user-data-dir=<Chrome-SecTest profile>

set -u

CDP_PORT="${CDP_PORT:-9223}"
PROFILE_DIR="$HOME/Library/Application Support/Chrome-SecTest"
PID_FILE="/tmp/chrome-sectest-${CDP_PORT}.pid"

_is_expected_pid() {
    local pid="$1"
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        return 1
    fi
    local cmd
    cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    [[ "${cmd}" != *"--type="* ]] || return 1
    [[ "${cmd}" == *"--remote-debugging-port=${CDP_PORT}"* ]] || return 1
    [[ "${cmd}" == *"--user-data-dir=${PROFILE_DIR}"* ]] || return 1
    return 0
}

_find_expected_pid() {
    local pid cmd
    while read -r pid cmd; do
        [ -n "${pid}" ] || continue
        [[ "${cmd}" != *"--type="* ]] || continue
        [[ "${cmd}" == *"--remote-debugging-port=${CDP_PORT}"* ]] || continue
        [[ "${cmd}" == *"--user-data-dir=${PROFILE_DIR}"* ]] || continue
        echo "${pid}"
        return 0
    done < <(ps ax -o pid= -o command=)
    return 1
}

target_pid=""
if [ -f "${PID_FILE}" ]; then
    candidate="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if _is_expected_pid "${candidate}"; then
        target_pid="${candidate}"
    else
        rm -f "${PID_FILE}"
    fi
fi

if [ -z "${target_pid}" ]; then
    if pid="$(_find_expected_pid)"; then
        target_pid="${pid}"
    fi
fi

if [ -z "${target_pid}" ]; then
    echo "ℹ️ Chrome-SecTest not running (nothing to stop)"
    exit 0
fi

echo "🛑 Stopping Chrome-SecTest pid=${target_pid} ..."
target_pgid="$(ps -p "${target_pid}" -o pgid= 2>/dev/null | tr -d ' ' || true)"
if [ -z "${target_pgid}" ]; then
    target_pgid="${target_pid}"
fi
kill -TERM "-${target_pgid}" 2>/dev/null || true

for _ in $(seq 1 10); do
    if ! kill -0 "-${target_pgid}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        echo "✅ Chrome-SecTest stopped"
        exit 0
    fi
    sleep 0.5
done

echo "⚠️ Graceful stop timeout, force killing pid=${target_pid}"
kill -KILL "-${target_pgid}" 2>/dev/null || true
rm -f "${PID_FILE}"
echo "✅ Chrome-SecTest force stopped"
exit 0
