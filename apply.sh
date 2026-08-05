#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./apply.sh [all|server|runner]

Build static UI assets, apply the PostgreSQL Alembic branch, and restart AXIOM.
EOF
}
TARGET="${1:-all}"
case "$TARGET" in all|server|runner) ;; -h|--help) usage; exit 0 ;; *) usage >&2; exit 2 ;; esac

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
AXIOM_DIR="${AXIOM_CODE_DIR:-$ROOT_DIR/axiom}"
AXIOM_DATA_DIR="${AXIOM_DATA_DIR:-$HOME/.local/share/axiom-v3}"
AXIOM_BIN="$AXIOM_DIR/.venv/bin/axiom"
STATIC_DIR="$AXIOM_DIR/src/axiom/static"
LOCK_DIR="$AXIOM_DATA_DIR/run/apply.lock"
# shellcheck disable=SC1091
source "$AXIOM_DIR/ops/postgres/common.sh"
load_axiom_postgres_env

mkdir -p "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[axiom] 已有另一个应用过程正在运行：$LOCK_DIR" >&2
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

require_file() { [[ -e "$1" ]] || { echo "[axiom] 缺少必要文件：$1" >&2; exit 1; }; }
build_tailwind_if_needed() {
  local output="$STATIC_DIR/tailwind.generated.css" needs_build=0
  if [[ ! -s "$output" ]]; then
    needs_build=1
  elif find "$AXIOM_DIR/tailwind.config.js" "$STATIC_DIR/tailwind.input.css" \
      "$STATIC_DIR/tailwind.safelist.html" "$STATIC_DIR/index.html" \
      "$STATIC_DIR/templates" "$STATIC_DIR/js" -type f -newer "$output" -print -quit \
      | grep -q .; then
    needs_build=1
  fi
  [[ "$needs_build" -eq 1 ]] || { echo "[axiom] Tailwind 已是最新"; return; }
  if [[ ! -x "$AXIOM_DIR/node_modules/.bin/tailwindcss" ]]; then
    command -v npm >/dev/null 2>&1 || { echo "[axiom] 找不到 npm" >&2; exit 1; }
    (cd "$AXIOM_DIR" && npm ci --ignore-scripts --no-audit --no-fund)
  fi
  (cd "$AXIOM_DIR" && npm run build:css)
}
refresh_static_version() {
  local version
  version="$(python3 - "$STATIC_DIR" <<'PY'
import hashlib,sys
from pathlib import Path
root=Path(sys.argv[1]); digest=hashlib.sha256()
for path in sorted(root.rglob('*')):
    if path.is_file() and path.name not in {'index.html','index-legacy.html'}:
        digest.update(path.relative_to(root).as_posix().encode()); digest.update(b'\0')
        digest.update(path.read_bytes()); digest.update(b'\0')
print(digest.hexdigest()[:12])
PY
)"
  python3 - "$STATIC_DIR" "$version" <<'PY'
import re,sys
from pathlib import Path
root=Path(sys.argv[1]); version=sys.argv[2]
pattern=re.compile(r'((?:src|href)="/static/[^"?]+)(?:\?v=[^"]*)?(")')
for name in ('index.html','index-legacy.html'):
    path=root/name
    if path.exists(): path.write_text(pattern.sub(lambda m:f'{m.group(1)}?v={version}{m.group(2)}',path.read_text()))
PY
  echo "[axiom] 静态资源版本：$version"
}

sync_axiom_runtime
require_file "$AXIOM_BIN"; require_file "$ROOT_DIR/restart.sh"; require_file "$AXIOM_DIR/package.json"
build_tailwind_if_needed
refresh_static_version
axiom_compose up -d postgres
wait_for_postgres
echo "[axiom] 应用 PostgreSQL 迁移"
"$AXIOM_BIN" db upgrade --database-url "$AXIOM_MIGRATOR_DATABASE_URL"
"$ROOT_DIR/restart.sh" "$TARGET"
echo "[axiom] 修改已应用"
