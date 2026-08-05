#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: cutover.sh --sqlite PATH --source-data-dir PATH --legacy-bin PATH

Performs the stopped-write SQLite -> PostgreSQL production cutover.  The source
runtime must still be the frozen SQLite commit; run this script from the tested
PostgreSQL worktree.  Before the first PostgreSQL write, any failure restores
preserved SQLite LaunchAgents.  After PostgreSQL is opened for writes, rollback
to SQLite is deliberately disabled.
EOF
  exit 2
}
SQLITE_PATH=""; SOURCE_DATA_DIR=""; LEGACY_BIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sqlite) SQLITE_PATH="$2"; shift 2 ;;
    --source-data-dir) SOURCE_DATA_DIR="$2"; shift 2 ;;
    --legacy-bin) LEGACY_BIN="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -s "$SQLITE_PATH" && -d "$SOURCE_DATA_DIR" && -x "$LEGACY_BIN" ]] || usage

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"
load_axiom_postgres_env
require_command curl
require_command launchctl
require_command docker
sync_axiom_runtime

API_URL="${AXIOM_API_URL:-http://127.0.0.1:8888}"
SOURCE_API_PORT="${AXIOM_SOURCE_SNAPSHOT_PORT:-18897}"
TARGET_API_PORT="${AXIOM_TARGET_SNAPSHOT_PORT:-18899}"
DRAIN_WAIT_SECONDS="${AXIOM_DRAIN_WAIT_SECONDS:-14400}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CUTOVER_DIR="$AXIOM_DATA_DIR/postgresql-migration/cutover-$STAMP"
FINAL_SQLITE="$CUTOVER_DIR/axiom.final.sqlite3"
WORKER_STATE="$CUTOVER_DIR/workers-before-drain.json"
SOURCE_SNAPSHOT="$CUTOVER_DIR/sqlite-api-snapshot.json"
TARGET_SNAPSHOT="$CUTOVER_DIR/postgresql-api-snapshot.json"
mkdir -p "$CUTOVER_DIR/launchagents"
umask 077

echo "$(git -C "$AXIOM_REPO_ROOT" rev-parse HEAD)" > "$CUTOVER_DIR/postgresql-git-commit.txt"
echo "$($LEGACY_BIN --version 2>/dev/null || true)" > "$CUTOVER_DIR/legacy-runtime-version.txt"
for plist in "$HOME/Library/LaunchAgents/com.xrayai.axiom.plist" "$HOME/Library/LaunchAgents/com.xrayai.axiom.runner.plist"; do
  [[ -f "$plist" ]] && cp -p "$plist" "$CUTOVER_DIR/launchagents/"
done

SOURCE_PID=""; TARGET_PID=""; WRITES_OPEN=0; WORKERS_RESTORED=0
stop_pid() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
}
restore_legacy_runtime() {
  echo "[axiom] restoring SQLite runtime before PostgreSQL write boundary" >&2
  launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.xrayai.axiom.runner.plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.xrayai.axiom.plist" >/dev/null 2>&1 || true
  for saved in "$CUTOVER_DIR"/launchagents/*.plist; do
    [[ -f "$saved" ]] || continue
    cp -p "$saved" "$HOME/Library/LaunchAgents/$(basename "$saved")"
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$(basename "$saved")" >/dev/null 2>&1 || true
  done
  if [[ "$WORKERS_RESTORED" -eq 0 && -s "$WORKER_STATE" ]]; then
    for _ in $(seq 1 60); do
      curl -fsS "$API_URL/health" >/dev/null 2>&1 && break
      sleep 1
    done
    "$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/drain_workers.py" restore \
      --api-url "$API_URL" --state-file "$WORKER_STATE" >/dev/null 2>&1 || true
  fi
  "$AXIOM_BIN" maintenance set normal --data-dir "$SOURCE_DATA_DIR" \
    --actor system.cutover.rollback --reason "pre-write cutover rollback" >/dev/null 2>&1 || true
}
cleanup() {
  local status=$?
  stop_pid "$SOURCE_PID"; stop_pid "$TARGET_PID"
  if [[ $status -ne 0 && "$WRITES_OPEN" -eq 0 ]]; then restore_legacy_runtime; fi
  exit "$status"
}
trap cleanup EXIT INT TERM

# Drain without killing active leases: disabled Workers cannot claim new work,
# while current lease heartbeats and completion endpoints remain valid.
"$AXIOM_BIN" maintenance set drain --data-dir "$SOURCE_DATA_DIR" \
  --actor system.cutover --reason "SQLite to PostgreSQL cutover" > "$CUTOVER_DIR/maintenance-drain.json"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/drain_workers.py" disable \
  --api-url "$API_URL" --state-file "$WORKER_STATE" > "$CUTOVER_DIR/workers-disabled.json"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/wait_sqlite_leases.py" \
  --sqlite-path "$SQLITE_PATH" --wait-seconds "$DRAIN_WAIT_SECONDS" \
  --output "$CUTOVER_DIR/final-active-leases.json" > "$CUTOVER_DIR/drain-progress.jsonl"

# Stop dispatch first, restore persisted Worker settings, then stop the API.
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.xrayai.axiom.runner.plist" >/dev/null 2>&1 || true
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/drain_workers.py" restore \
  --api-url "$API_URL" --state-file "$WORKER_STATE" > "$CUTOVER_DIR/workers-restored.json"
WORKERS_RESTORED=1
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.xrayai.axiom.plist" >/dev/null 2>&1 || true

# APSW bundles SQLite 3.51.3 and performs the final checkpoint and backup only
# after all writers are gone.
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/sqlite_snapshot.py" \
  --source "$SQLITE_PATH" --output "$FINAL_SQLITE" \
  --manifest "$CUTOVER_DIR/sqlite-final-manifest.json" --checkpoint > "$CUTOVER_DIR/sqlite-final-manifest.stdout.json"

# Snapshot the exact immutable SQLite copy with the preserved legacy API.
"$(dirname "$LEGACY_BIN")/python" "$SCRIPT_DIR/serve_legacy_snapshot.py" \
  --host 127.0.0.1 --port "$SOURCE_API_PORT" --sqlite-path "$FINAL_SQLITE" \
  --data-dir "$SOURCE_DATA_DIR" > "$CUTOVER_DIR/sqlite-api.log" 2>&1 &
SOURCE_PID="$!"
for _ in $(seq 1 90); do curl -fsS "http://127.0.0.1:$SOURCE_API_PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$SOURCE_API_PORT/health" >/dev/null
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/snapshot_api.py" \
  --api-url "http://127.0.0.1:$SOURCE_API_PORT" --output "$SOURCE_SNAPSHOT"
stop_pid "$SOURCE_PID"; SOURCE_PID=""

# PostgreSQL starts frozen and remains read-only to HTTP callers through all comparisons.
if [[ ! -f "$AXIOM_PG_ENV_FILE" ]]; then
  APP_PASSWORD="$(openssl rand -hex 24)"
  MIGRATOR_PASSWORD="$(openssl rand -hex 24)"
  BOOTSTRAP_PASSWORD="$(openssl rand -hex 24)"
  cat > "$AXIOM_PG_ENV_FILE" <<EOF
AXIOM_POSTGRES_IMAGE=postgres:18.4-bookworm@sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382
AXIOM_POSTGRES_BOOTSTRAP_PASSWORD=$BOOTSTRAP_PASSWORD
AXIOM_DB_NAME=axiom
AXIOM_DB_HOST=127.0.0.1
AXIOM_DB_PORT=55432
AXIOM_DB_USER=axiom_app
AXIOM_DB_PASSWORD=$APP_PASSWORD
AXIOM_DB_MIGRATOR_PASSWORD=$MIGRATOR_PASSWORD
AXIOM_DB_APP_PASSWORD=$APP_PASSWORD
AXIOM_DATABASE_URL=postgresql+psycopg://axiom_app:$APP_PASSWORD@127.0.0.1:55432/axiom
AXIOM_MIGRATOR_DATABASE_URL=postgresql+psycopg://axiom_migrator:$MIGRATOR_PASSWORD@127.0.0.1:55432/axiom
EOF
  chmod 600 "$AXIOM_PG_ENV_FILE"
  load_axiom_postgres_env
fi
axiom_compose up -d postgres
wait_for_postgres
postgres_recreate_empty_database "$AXIOM_DB_NAME"
"$AXIOM_BIN" db upgrade --database-url "$AXIOM_MIGRATOR_DATABASE_URL" > "$CUTOVER_DIR/alembic-upgrade.txt"
"$AXIOM_BIN" db migrate-sqlite --sqlite-path "$FINAL_SQLITE" \
  --database-url "$AXIOM_DATABASE_URL" --data-dir "$SOURCE_DATA_DIR" \
  --report-dir "$CUTOVER_DIR" > "$CUTOVER_DIR/import-command.json"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/database_fingerprint.py" \
  --database-url "$AXIOM_DATABASE_URL" --output "$CUTOVER_DIR/postgresql-fingerprint.json"
"$AXIOM_BIN" maintenance set frozen --data-dir "$SOURCE_DATA_DIR" \
  --actor system.cutover --reason "PostgreSQL validation before write boundary" >/dev/null
AXIOM_DATABASE_URL="$AXIOM_DATABASE_URL" "$AXIOM_BIN" serve \
  --host 127.0.0.1 --port "$TARGET_API_PORT" --data-dir "$SOURCE_DATA_DIR" \
  > "$CUTOVER_DIR/postgresql-api.log" 2>&1 &
TARGET_PID="$!"
for _ in $(seq 1 90); do curl -fsS "http://127.0.0.1:$TARGET_API_PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$TARGET_API_PORT/health" > "$CUTOVER_DIR/postgresql-health.json"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/snapshot_api.py" \
  --api-url "http://127.0.0.1:$TARGET_API_PORT" --output "$TARGET_SNAPSHOT"
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/compare_snapshots.py" \
  "$SOURCE_SNAPSHOT" "$TARGET_SNAPSHOT" --report "$CUTOVER_DIR/api-comparison.json" \
  > "$CUTOVER_DIR/api-comparison.stdout.json"
stop_pid "$TARGET_PID"; TARGET_PID=""

# Validate a full logical backup and restore while PostgreSQL is still frozen
# and has had no post-migration writes.  The restored table hashes must match
# the import fingerprint exactly before crossing the irreversible boundary.
"$SCRIPT_DIR/backup.sh" > "$CUTOVER_DIR/first-backup-paths.txt"
FIRST_DUMP="$(head -n 1 "$CUTOVER_DIR/first-backup-paths.txt")"
"$SCRIPT_DIR/restore_verify.sh" \
  "$FIRST_DUMP" "$CUTOVER_DIR/postgresql-fingerprint.json" \
  > "$CUTOVER_DIR/first-restore-verify.txt"

# This is the irreversible boundary: start.sh opens PostgreSQL for writes and starts Runner.
AXIOM_CODE_DIR="$AXIOM_DIR" AXIOM_DATA_DIR="$SOURCE_DATA_DIR" \
  AXIOM_PG_ENV_FILE="$AXIOM_PG_ENV_FILE" "$AXIOM_REPO_ROOT/start.sh" start \
  > "$CUTOVER_DIR/start-postgresql.log" 2>&1
WRITES_OPEN=1
curl -fsS "$API_URL/health" > "$CUTOVER_DIR/production-health.json"
"$SCRIPT_DIR/install_backup_launchagent.sh" > "$CUTOVER_DIR/backup-launchagent.txt"

OBSERVATION_DIR="$CUTOVER_DIR/observation-24h"
"$SCRIPT_DIR/install_observer_launchagent.sh" \
  --output-dir "$OBSERVATION_DIR" --duration-seconds 86400 --interval-seconds 60 \
  > "$CUTOVER_DIR/observer-launchagent.txt"
OBSERVATION_PID="$(
  launchctl print "gui/$(id -u)/com.xrayai.axiom.postgresql-observer" 2>/dev/null \
    | awk -F'= ' '/pid = / {print $2; exit}'
)"
printf '%s\n' "$OBSERVATION_PID" > "$OBSERVATION_DIR.pid"
cat > "$CUTOVER_DIR/CUTOVER_COMPLETE.json" <<EOF
{"completed_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","postgresql_writes_open":true,"sqlite_rollback_allowed":false,"observation_pid":${OBSERVATION_PID:-null}}
EOF

echo "$CUTOVER_DIR"
