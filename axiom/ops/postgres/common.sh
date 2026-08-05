#!/usr/bin/env bash
set -euo pipefail

AXIOM_OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AXIOM_DIR="${AXIOM_CODE_DIR:-$(cd "$AXIOM_OPS_DIR/../.." && pwd)}"
AXIOM_REPO_ROOT="${AXIOM_REPO_ROOT:-$(cd "$AXIOM_DIR/.." && pwd)}"
AXIOM_DATA_DIR="${AXIOM_DATA_DIR:-$HOME/.local/share/axiom-v3}"
AXIOM_PG_ENV_FILE="${AXIOM_PG_ENV_FILE:-$AXIOM_DATA_DIR/postgresql.env}"
AXIOM_BIN="${AXIOM_BIN:-$AXIOM_DIR/.venv/bin/axiom}"

load_axiom_postgres_env() {
  if [[ -f "$AXIOM_PG_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$AXIOM_PG_ENV_FILE"
    set +a
  fi
  export AXIOM_DB_HOST="${AXIOM_DB_HOST:-127.0.0.1}"
  export AXIOM_DB_PORT="${AXIOM_DB_PORT:-55432}"
  export AXIOM_DB_NAME="${AXIOM_DB_NAME:-axiom}"
  export AXIOM_DB_USER="${AXIOM_DB_USER:-axiom_app}"
  export AXIOM_DB_PASSWORD="${AXIOM_DB_PASSWORD:-axiom-app-local}"
  export AXIOM_DB_MIGRATOR_PASSWORD="${AXIOM_DB_MIGRATOR_PASSWORD:-axiom-migrator-local}"
  export AXIOM_DB_APP_PASSWORD="${AXIOM_DB_APP_PASSWORD:-$AXIOM_DB_PASSWORD}"
  export AXIOM_DATABASE_URL="${AXIOM_DATABASE_URL:-postgresql+psycopg://$AXIOM_DB_USER:$AXIOM_DB_PASSWORD@$AXIOM_DB_HOST:$AXIOM_DB_PORT/$AXIOM_DB_NAME}"
  export AXIOM_MIGRATOR_DATABASE_URL="${AXIOM_MIGRATOR_DATABASE_URL:-postgresql+psycopg://axiom_migrator:$AXIOM_DB_MIGRATOR_PASSWORD@$AXIOM_DB_HOST:$AXIOM_DB_PORT/$AXIOM_DB_NAME}"
  export AXIOM_POSTGRES_IMAGE="${AXIOM_POSTGRES_IMAGE:-postgres:18.4-bookworm@sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382}"
}

axiom_compose() {
  docker compose \
    --project-directory "$AXIOM_DIR" \
    -f "$AXIOM_DIR/compose.yaml" \
    -f "$AXIOM_DIR/compose.host.yaml" \
    "$@"
}

wait_for_postgres() {
  local attempts="${1:-90}"
  for _ in $(seq 1 "$attempts"); do
    if axiom_compose exec -T postgres pg_isready -U axiom_bootstrap -d postgres >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[axiom] PostgreSQL did not become ready" >&2
  return 1
}

postgres_database_url() {
  local url="$1" database="$2"
  "$AXIOM_DIR/.venv/bin/python" - "$url" "$database" <<'PY'
import sys
from sqlalchemy.engine import make_url
print(make_url(sys.argv[1]).set(database=sys.argv[2]).render_as_string(hide_password=False))
PY
}

postgres_create_database() {
  local database="$1"
  axiom_compose exec -T postgres psql -v ON_ERROR_STOP=1 -U axiom_bootstrap -d postgres \
    --set=database_name="$database" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER axiom_owner', :'database_name')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name')\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO axiom_migrator, axiom_app', :'database_name')\gexec
SQL
}

postgres_drop_database() {
  local database="$1"
  axiom_compose exec -T postgres psql -v ON_ERROR_STOP=1 -U axiom_bootstrap -d postgres \
    --set=database_name="$database" <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = :'database_name' AND pid <> pg_backend_pid();
SELECT format('DROP DATABASE IF EXISTS %I', :'database_name')\gexec
SQL
}

postgres_recreate_empty_database() {
  local database="$1"
  local exists
  exists="$(
    axiom_compose exec -T postgres psql -At -U axiom_bootstrap -d postgres \
      --set=database_name="$database" <<'SQL'
SELECT 1 FROM pg_database WHERE datname = :'database_name';
SQL
  )"
  exists="$(
    printf '%s' "$exists" \
      | tr -d '[:space:]'
  )"
  if [[ "$exists" == "1" ]]; then
    axiom_compose exec -T postgres psql -v ON_ERROR_STOP=1 -U axiom_bootstrap \
      -d "$database" <<'SQL'
DO $$
DECLARE
  item record;
  row_count bigint;
BEGIN
  FOR item IN
    SELECT schemaname, tablename
    FROM pg_tables
    WHERE schemaname = 'axiom' AND tablename <> 'alembic_version'
  LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I', item.schemaname, item.tablename)
      INTO row_count;
    IF row_count <> 0 THEN
      RAISE EXCEPTION 'refusing to reset non-empty target: %.% has % rows',
        item.schemaname, item.tablename, row_count;
    END IF;
  END LOOP;
END
$$;
SQL
    postgres_drop_database "$database"
  fi
  postgres_create_database "$database"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[axiom] missing command: $1" >&2
    exit 1
  }
}

sync_axiom_runtime() {
  require_command uv
  (cd "$AXIOM_DIR" && uv sync --frozen)
}
