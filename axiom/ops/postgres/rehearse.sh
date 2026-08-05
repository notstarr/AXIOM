#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: rehearse.sh --sqlite PATH --data-dir PATH --source-api-snapshot PATH --name NAME
EOF
  exit 2
}
SQLITE_PATH=""; SOURCE_DATA_DIR=""; SOURCE_API_SNAPSHOT=""; NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sqlite) SQLITE_PATH="$2"; shift 2 ;;
    --data-dir) SOURCE_DATA_DIR="$2"; shift 2 ;;
    --source-api-snapshot) SOURCE_API_SNAPSHOT="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -s "$SQLITE_PATH" && -d "$SOURCE_DATA_DIR" && -s "$SOURCE_API_SNAPSHOT" && -n "$NAME" ]] || usage

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"
umask 077
load_axiom_postgres_env
axiom_compose up -d postgres
wait_for_postgres

SAFE_NAME="$(printf '%s' "$NAME" | tr -cd '[:alnum:]_' | tr '[:upper:]' '[:lower:]')"
DATABASE="axiom_rehearsal_${SAFE_NAME}"
REPORT_DIR="$AXIOM_DATA_DIR/postgresql-migration/$NAME"
RUNTIME_DATA_DIR="$REPORT_DIR/runtime-data"
APP_URL="$(postgres_database_url "$AXIOM_DATABASE_URL" "$DATABASE")"
MIGRATOR_URL="$(postgres_database_url "$AXIOM_MIGRATOR_DATABASE_URL" "$DATABASE")"
PORT="${AXIOM_REHEARSAL_PORT:-18899}"
PID=""
mkdir -p "$REPORT_DIR" "$RUNTIME_DATA_DIR"
rm -rf "$RUNTIME_DATA_DIR/skills" "$RUNTIME_DATA_DIR/axiom-tools"
[[ ! -d "$SOURCE_DATA_DIR/skills" ]] || cp -R "$SOURCE_DATA_DIR/skills" "$RUNTIME_DATA_DIR/skills"
[[ ! -d "$SOURCE_DATA_DIR/axiom-tools" ]] || cp -R "$SOURCE_DATA_DIR/axiom-tools" "$RUNTIME_DATA_DIR/axiom-tools"

cleanup() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  if [[ "${AXIOM_KEEP_REHEARSAL_DATABASE:-0}" != "1" ]]; then
    postgres_drop_database "$DATABASE" >/dev/null 2>&1 || true
  fi
  "$AXIOM_BIN" maintenance set normal --data-dir "$RUNTIME_DATA_DIR" \
    --actor system.rehearsal --reason "rehearsal cleanup" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
postgres_drop_database "$DATABASE" >/dev/null 2>&1 || true
postgres_create_database "$DATABASE"
"$AXIOM_BIN" db upgrade --database-url "$MIGRATOR_URL" > "$REPORT_DIR/alembic-upgrade.txt"
"$AXIOM_BIN" db migrate-sqlite --sqlite-path "$SQLITE_PATH" \
  --database-url "$APP_URL" --data-dir "$SOURCE_DATA_DIR" --report-dir "$REPORT_DIR" \
  > "$REPORT_DIR/import-command.json"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/database_fingerprint.py" \
  --database-url "$APP_URL" --output "$REPORT_DIR/database-fingerprint.json"

"$AXIOM_BIN" maintenance set frozen --data-dir "$RUNTIME_DATA_DIR" \
  --actor system.rehearsal --reason "$NAME" > "$REPORT_DIR/maintenance.json"
AXIOM_DATABASE_URL="$APP_URL" "$AXIOM_BIN" serve --host 127.0.0.1 --port "$PORT" \
  --data-dir "$RUNTIME_DATA_DIR" > "$REPORT_DIR/api.log" 2>&1 &
PID="$!"
for _ in $(seq 1 90); do
  curl -fsS "http://127.0.0.1:$PORT/health" > "$REPORT_DIR/health.json" 2>/dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/snapshot_api.py" \
  --api-url "http://127.0.0.1:$PORT" --output "$REPORT_DIR/postgresql-api-snapshot.json"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/compare_snapshots.py" \
  "$SOURCE_API_SNAPSHOT" "$REPORT_DIR/postgresql-api-snapshot.json" \
  --report "$REPORT_DIR/api-comparison.json" > "$REPORT_DIR/api-comparison.stdout.json"
"$AXIOM_BIN" maintenance set normal --data-dir "$RUNTIME_DATA_DIR" \
  --actor system.rehearsal --reason "rehearsal complete" >/dev/null

echo "$REPORT_DIR"
