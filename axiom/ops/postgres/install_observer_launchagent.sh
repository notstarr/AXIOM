#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: install_observer_launchagent.sh --output-dir PATH [--duration-seconds N] [--interval-seconds N]
EOF
  exit 2
}

OUTPUT_DIR=""
DURATION_SECONDS=86400
INTERVAL_SECONDS=60
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --duration-seconds) DURATION_SECONDS="$2"; shift 2 ;;
    --interval-seconds) INTERVAL_SECONDS="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$OUTPUT_DIR" && "$DURATION_SECONDS" =~ ^[0-9]+$ && "$INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || usage

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"
load_axiom_postgres_env

LABEL="com.xrayai.axiom.postgresql-observer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$AXIOM_DATA_DIR/logs"
PYTHON="$AXIOM_DIR/.venv/bin/python"
mkdir -p "$(dirname "$PLIST")" "$LOG_DIR" "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

python3 - \
  "$PLIST" "$LABEL" "$PYTHON" "$SCRIPT_DIR/observe_postgresql.py" \
  "$AXIOM_DIR" "$AXIOM_DATA_DIR" "$AXIOM_PG_ENV_FILE" \
  "$LOG_DIR/postgresql-observer.log" "$OUTPUT_DIR" \
  "$DURATION_SECONDS" "$INTERVAL_SECONDS" <<'PY'
import os
import plistlib
import sys

(
    plist,
    label,
    python,
    observer,
    workdir,
    data_dir,
    env_file,
    log_path,
    output_dir,
    duration,
    interval,
) = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [
        python,
        observer,
        "--api-url",
        "http://127.0.0.1:8888",
        "--duration-seconds",
        duration,
        "--interval-seconds",
        interval,
        "--output-dir",
        output_dir,
    ],
    "WorkingDirectory": workdir,
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
    "RunAtLoad": True,
    "KeepAlive": False,
    "ProcessType": "Background",
    "EnvironmentVariables": {
        "AXIOM_CODE_DIR": workdir,
        "AXIOM_DATA_DIR": data_dir,
        "AXIOM_PG_ENV_FILE": env_file,
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
}
with open(plist, "wb") as handle:
    plistlib.dump(payload, handle)
os.chmod(plist, 0o600)
PY

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
echo "$PLIST"
