#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 /path/to/axiom-YYYYMMDDTHHMMSSZ.dump [expected-fingerprint.json]" >&2
  exit 2
}
[[ $# -ge 1 && $# -le 2 ]] || usage
DUMP_PATH="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
[[ -s "$DUMP_PATH" ]] || { echo "backup not found: $DUMP_PATH" >&2; exit 1; }
EXPECTED_FINGERPRINT=""
if [[ $# -eq 2 ]]; then
  EXPECTED_FINGERPRINT="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
  [[ -s "$EXPECTED_FINGERPRINT" ]] || {
    echo "expected fingerprint not found: $EXPECTED_FINGERPRINT" >&2
    exit 1
  }
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"
load_axiom_postgres_env
wait_for_postgres

STAMP="$(date -u +%Y%m%d%H%M%S)"
DATABASE="axiom_restore_$STAMP"
REPORT_DIR="${AXIOM_PG_RESTORE_REPORT_DIR:-$AXIOM_DATA_DIR/postgresql-migration/restore-$STAMP}"
APP_URL="$(postgres_database_url "$AXIOM_DATABASE_URL" "$DATABASE")"
PORT="${AXIOM_RESTORE_VERIFY_PORT:-18898}"
PID=""
mkdir -p "$REPORT_DIR"

cleanup() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  postgres_drop_database "$DATABASE" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

postgres_create_database "$DATABASE"
cat "$DUMP_PATH" | axiom_compose exec -T postgres \
  pg_restore --exit-on-error --no-password --username axiom_bootstrap --dbname "$DATABASE"

"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/database_fingerprint.py" \
  --database-url "$APP_URL" --output "$REPORT_DIR/database-fingerprint.json"
if [[ -n "$EXPECTED_FINGERPRINT" ]]; then
  "$AXIOM_DIR/.venv/bin/python" - \
    "$EXPECTED_FINGERPRINT" "$REPORT_DIR/database-fingerprint.json" <<'PY'
import json
import sys
from pathlib import Path

expected = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
actual = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if expected != actual:
    raise SystemExit("restored database fingerprint does not match the source")
print("database fingerprint: MATCH")
PY
fi

VERIFY_DATA="$REPORT_DIR/data"
mkdir -p "$VERIFY_DATA"
"$AXIOM_BIN" maintenance set frozen --data-dir "$VERIFY_DATA" \
  --actor system.restore-verify --reason "backup restore verification" >/dev/null
AXIOM_DATABASE_URL="$APP_URL" "$AXIOM_BIN" serve \
  --host 127.0.0.1 --port "$PORT" --data-dir "$VERIFY_DATA" \
  >"$REPORT_DIR/api.log" 2>&1 &
PID="$!"
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/health" > "$REPORT_DIR/health.json" 2>/dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null
"$AXIOM_DIR/.venv/bin/python" "$SCRIPT_DIR/snapshot_api.py" \
  --api-url "http://127.0.0.1:$PORT" --output "$REPORT_DIR/api-snapshot.json"

echo "$REPORT_DIR"
