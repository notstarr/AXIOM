#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./apply.sh [all|server|runner]

Build the static UI, refresh browser cache versions, apply database migrations,
and restart the selected AXIOM services. The default target is "all".

  all      apply everything and restart API + Runner (default)
  server   apply everything and restart only the API
  runner   apply everything and restart only the Runner
EOF
}

TARGET="${1:-all}"
case "$TARGET" in
  all|server|runner) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
AXIOM_DIR="${AXIOM_CODE_DIR:-$ROOT_DIR/axiom}"
AXIOM_DATA_DIR="${AXIOM_DATA_DIR:-$HOME/.local/share/axiom-v3}"
AXIOM_DB="${AXIOM_DB:-$AXIOM_DATA_DIR/axiom.db}"
AXIOM_BIN="$AXIOM_DIR/.venv/bin/axiom"
STATIC_DIR="$AXIOM_DIR/src/axiom/static"
LOCK_DIR="$AXIOM_DATA_DIR/run/apply.lock"

mkdir -p "$(dirname "$LOCK_DIR")"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[axiom] 已有另一个应用过程正在运行：$LOCK_DIR" >&2
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

require_file() {
  [[ -e "$1" ]] || {
    echo "[axiom] 缺少必要文件：$1" >&2
    exit 1
  }
}

build_tailwind_if_needed() {
  local output="$STATIC_DIR/tailwind.generated.css"
  local needs_build=0

  if [[ ! -s "$output" ]]; then
    needs_build=1
  elif find \
      "$AXIOM_DIR/tailwind.config.js" \
      "$STATIC_DIR/tailwind.input.css" \
      "$STATIC_DIR/tailwind.safelist.html" \
      "$STATIC_DIR/index.html" \
      "$STATIC_DIR/templates" \
      "$STATIC_DIR/js" \
      -type f -newer "$output" -print -quit | grep -q .; then
    needs_build=1
  fi

  if [[ "$needs_build" -eq 0 ]]; then
    echo "[axiom] Tailwind 已是最新"
    return
  fi

  if [[ ! -x "$AXIOM_DIR/node_modules/.bin/tailwindcss" ]]; then
    command -v npm >/dev/null 2>&1 || {
      echo "[axiom] Tailwind 需要重新构建，但当前找不到 npm" >&2
      exit 1
    }
    echo "[axiom] 安装固定版本的前端构建依赖"
    (cd "$AXIOM_DIR" && npm ci --ignore-scripts --no-audit --no-fund)
  fi

  echo "[axiom] 构建静态 Tailwind CSS"
  (cd "$AXIOM_DIR" && npm run build:css)
}

refresh_static_version() {
  local version
  version="$(python3 - "$STATIC_DIR" <<'PY'
import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.name in {"index.html", "index-legacy.html"}:
        continue
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest()[:12])
PY
)"

  python3 - "$STATIC_DIR" "$version" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]
pattern = re.compile(r'((?:src|href)="/static/[^"?]+)(?:\?v=[^"]*)?(")')

for name in ("index.html", "index-legacy.html"):
    path = root / name
    if not path.exists():
        continue
    original = path.read_text()
    updated = pattern.sub(lambda match: f"{match.group(1)}?v={version}{match.group(2)}", original)
    if updated != original:
        path.write_text(updated)
PY
  echo "[axiom] 静态资源版本：$version"
}

require_file "$AXIOM_BIN"
require_file "$ROOT_DIR/restart.sh"
require_file "$AXIOM_DIR/package.json"
require_file "$STATIC_DIR/index.html"

build_tailwind_if_needed
refresh_static_version

echo "[axiom] 应用数据库迁移"
"$AXIOM_BIN" db-upgrade --db-path "$AXIOM_DB"

echo "[axiom] 重启目标：$TARGET"
"$ROOT_DIR/restart.sh" "$TARGET"

echo "[axiom] 修改已应用。浏览器刷新页面即可加载最新静态资源。"
