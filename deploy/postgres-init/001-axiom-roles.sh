#!/usr/bin/env bash
set -euo pipefail

psql --username "$POSTGRES_USER" --dbname postgres \
  --set=migrator_password="$AXIOM_DB_MIGRATOR_PASSWORD" \
  --set=app_password="$AXIOM_DB_APP_PASSWORD" <<'SQL'
CREATE ROLE axiom_owner NOLOGIN;
CREATE ROLE axiom_migrator LOGIN PASSWORD :'migrator_password';
CREATE ROLE axiom_app LOGIN PASSWORD :'app_password';
GRANT axiom_owner TO axiom_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL

psql --username "$POSTGRES_USER" --dbname postgres \
  --set=database_name="$AXIOM_DB_NAME" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER axiom_owner', :'database_name')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name')\gexec
SQL

psql --username "$POSTGRES_USER" --dbname "$AXIOM_DB_NAME" \
  --set=database_name="$AXIOM_DB_NAME" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO axiom_migrator, axiom_app;
ALTER ROLE axiom_migrator SET timezone = 'UTC';
ALTER ROLE axiom_app SET timezone = 'UTC';
SQL
