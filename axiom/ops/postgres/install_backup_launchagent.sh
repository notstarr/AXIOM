#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"
load_axiom_postgres_env

LABEL="com.xrayai.axiom.postgresql-backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$AXIOM_DATA_DIR/logs"
mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"

python3 - "$PLIST" "$LABEL" "$SCRIPT_DIR/backup.sh" "$AXIOM_DIR" "$AXIOM_DATA_DIR" "$AXIOM_PG_ENV_FILE" "$LOG_DIR/postgresql-backup.log" <<'PY'
import os, plistlib, sys
plist,label,program,workdir,data_dir,env_file,log_path=sys.argv[1:]
payload={
    'Label': label,
    'ProgramArguments': [program],
    'WorkingDirectory': workdir,
    'RunAtLoad': False,
    'StartCalendarInterval': {'Hour': 3, 'Minute': 15},
    'StandardOutPath': log_path,
    'StandardErrorPath': log_path,
    'EnvironmentVariables': {
        'AXIOM_CODE_DIR': workdir,
        'AXIOM_DATA_DIR': data_dir,
        'AXIOM_PG_ENV_FILE': env_file,
        'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    },
}
with open(plist,'wb') as handle: plistlib.dump(payload,handle)
os.chmod(plist, 0o600)
PY
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "$PLIST"
