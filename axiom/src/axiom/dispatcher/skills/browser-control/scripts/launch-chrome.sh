#!/bin/bash
# Launch Chrome-SecTest profile with CDP port 9223.
# When MITM_PROXY=1, also routes traffic through mitmproxy on port 18080.
# Safe to call multiple times — if already running, does nothing.
#
# Safety hardening:
# - Never treat an arbitrary Chrome on the same port as Chrome-SecTest.
# - If port is occupied by a non-SecTest Chrome, fail fast instead of hijacking.

set -u

CDP_PORT="${CDP_PORT:-9223}"
PROFILE_DIR="$HOME/Library/Application Support/Chrome-SecTest"
CHROME="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PYTHON="${PYTHON_BIN:-python3}"
PID_FILE="/tmp/chrome-sectest-${CDP_PORT}.pid"

_cdp_alive() {
    curl -s "http://localhost:${CDP_PORT}/json/version" >/dev/null 2>&1
}

_is_expected_pid() {
    local pid="$1"
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        return 1
    fi
    local cmd
    cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    [[ "${cmd}" == *"--remote-debugging-port=${CDP_PORT}"* ]] || return 1
    [[ "${cmd}" == *"--user-data-dir=${PROFILE_DIR}"* ]] || return 1
    return 0
}

_find_expected_pid() {
    local line
    while IFS= read -r line; do
        local pid="${line%% *}"
        local cmd="${line#* }"
        [[ "${cmd}" == *"--remote-debugging-port=${CDP_PORT}"* ]] || continue
        [[ "${cmd}" == *"--user-data-dir=${PROFILE_DIR}"* ]] || continue
        echo "${pid}"
        return 0
    done < <(ps ax -o pid= -o command=)
    return 1
}

mkdir -p "${PROFILE_DIR}"

if [ -f "${PID_FILE}" ]; then
    existing_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if _is_expected_pid "${existing_pid}" && _cdp_alive; then
        echo "✅ Chrome-SecTest already running on port ${CDP_PORT} (pid=${existing_pid})"
        exit 0
    fi
    rm -f "${PID_FILE}"
fi

if _cdp_alive; then
    # Port is up, but if it's not our dedicated profile process, do not hijack it.
    if pid="$(_find_expected_pid)"; then
        echo "${pid}" > "${PID_FILE}"
        echo "✅ Chrome-SecTest already running on port ${CDP_PORT} (pid=${pid})"
        exit 0
    fi
    echo "❌ Port ${CDP_PORT} is occupied by a non-SecTest Chrome instance."
    echo "   Refusing to attach to avoid closing your personal browser tabs."
    echo "   Please free this port or run with another CDP_PORT."
    exit 2
fi

echo "🚀 Launching Chrome-SecTest..."

_launch_chrome() {
    new_pid="$(
        "${PYTHON}" - \
          "${CHROME}" \
          "--user-data-dir=${PROFILE_DIR}" \
          "--remote-debugging-port=${CDP_PORT}" \
          "--remote-allow-origins=*" \
          "--no-first-run" \
          "--window-size=1280,800" \
          "$@" <<'PY'
import subprocess
import sys

process = subprocess.Popen(
    sys.argv[1:],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
print(process.pid)
PY
    )" || return 1
    case "${new_pid}" in
        ''|*[!0-9]*)
            echo "❌ Chrome-SecTest launcher returned an invalid pid: ${new_pid}"
            return 1
            ;;
    esac
}

if [ "${MITM_PROXY:-0}" = "1" ]; then
    echo "🔀 mitmproxy mode: traffic → 127.0.0.1:18080"
    _launch_chrome \
      --proxy-server=http://127.0.0.1:18080 \
      --ignore-certificate-errors
else
    _launch_chrome
fi

# Wait up to 10s for port to be ready, then discover Chrome's actual process.
# Chrome may replace its initial process during startup, so do not trust new_pid
# as the durable PID until the command line has been verified.
for i in $(seq 1 10); do
    sleep 1
    if _cdp_alive; then
        ready_pid="$(_find_expected_pid || true)"
        if [ -n "${ready_pid}" ]; then
            echo "${ready_pid}" > "${PID_FILE}"
            echo "✅ Chrome-SecTest ready (${i}s, pid=${ready_pid})"
            exit 0
        fi
    fi
done

echo "❌ Chrome-SecTest failed to start"
rm -f "${PID_FILE}"
cleanup_pid=""
if _is_expected_pid "${new_pid}"; then
    cleanup_pid="${new_pid}"
else
    cleanup_pid="$(_find_expected_pid || true)"
fi
if [ -n "${cleanup_pid}" ]; then
    kill -TERM "${cleanup_pid}" 2>/dev/null || true
fi
exit 1
