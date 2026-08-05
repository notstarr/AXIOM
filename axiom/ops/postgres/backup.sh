#!/usr/bin/env bash
set -euo pipefail

# PostgreSQL backups intentionally contain AXIOM's plaintext credentials and API keys.
# Access control is provided by PostgreSQL and filesystem permissions, per deployment policy.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"
load_axiom_postgres_env
wait_for_postgres

BACKUP_ROOT="${AXIOM_PG_BACKUP_DIR:-$AXIOM_DATA_DIR/postgresql-backups}"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DAILY_DUMP="$DAILY_DIR/axiom-$STAMP.dump"
GLOBALS_DUMP="$DAILY_DIR/globals-$STAMP.sql"
MANIFEST="$DAILY_DIR/manifest-$STAMP.json"
mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"
umask 077

axiom_compose exec -T postgres \
  pg_dump --format=custom --compress=9 --no-password \
  --username axiom_bootstrap --dbname "$AXIOM_DB_NAME" > "$DAILY_DUMP"
axiom_compose exec -T postgres \
  pg_dumpall --globals-only --no-password \
  --username axiom_bootstrap > "$GLOBALS_DUMP"

"$AXIOM_DIR/.venv/bin/python" - "$DAILY_DUMP" "$GLOBALS_DUMP" "$MANIFEST" "$AXIOM_DB_NAME" <<'PY'
import hashlib, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

def digest(path: Path) -> str:
    value=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()

dump = Path(sys.argv[1])
globals_dump = Path(sys.argv[2])
manifest = Path(sys.argv[3])
database = sys.argv[4]
payload={
    'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
    'database': database,
    'plaintext_secrets': True,
    'files': [
        {'path': str(dump), 'size': dump.stat().st_size, 'sha256': digest(dump)},
        {'path': str(globals_dump), 'size': globals_dump.stat().st_size, 'sha256': digest(globals_dump)},
    ],
}
manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
os.chmod(manifest, 0o600)
PY
chmod 600 "$DAILY_DUMP" "$GLOBALS_DUMP" "$MANIFEST"

# Keep 14 daily sets.  Python avoids locale-dependent shell date parsing.
"$AXIOM_DIR/.venv/bin/python" - "$DAILY_DIR" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
stamps=sorted({p.stem.removeprefix('manifest-') for p in root.glob('manifest-*.json')}, reverse=True)
for stamp in stamps[14:]:
    for prefix, suffix in (('axiom-', '.dump'), ('globals-', '.sql'), ('manifest-', '.json')):
        (root / f'{prefix}{stamp}{suffix}').unlink(missing_ok=True)
PY

# The first successful backup on Sunday is copied into the weekly tier; keep eight.
if [[ "$(date -u +%u)" == "7" ]]; then
  cp -p "$DAILY_DUMP" "$WEEKLY_DIR/axiom-$STAMP.dump"
  cp -p "$GLOBALS_DUMP" "$WEEKLY_DIR/globals-$STAMP.sql"
  cp -p "$MANIFEST" "$WEEKLY_DIR/manifest-$STAMP.json"
fi
"$AXIOM_DIR/.venv/bin/python" - "$WEEKLY_DIR" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
stamps=sorted({p.stem.removeprefix('manifest-') for p in root.glob('manifest-*.json')}, reverse=True)
for stamp in stamps[8:]:
    for prefix, suffix in (('axiom-', '.dump'), ('globals-', '.sql'), ('manifest-', '.json')):
        (root / f'{prefix}{stamp}{suffix}').unlink(missing_ok=True)
PY

echo "$DAILY_DUMP"
echo "$GLOBALS_DUMP"
echo "$MANIFEST"
