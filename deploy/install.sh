#!/usr/bin/env bash
# AXIOM Linux installer. Safe to run through `curl | bash` or after review.
set -Eeuo pipefail

AXIOM_VERSION="${AXIOM_VERSION:-}"
if [[ -z "$AXIOM_INSTALL_DIR" ]]; then
  if [[ "$(uname -s)" == Darwin ]]; then
    AXIOM_INSTALL_DIR="$HOME/.axiom"
  else
    AXIOM_INSTALL_DIR="/opt/axiom"
  fi
fi
AXIOM_PUBLISH_HOST="${AXIOM_PUBLISH_HOST:-127.0.0.1}"
AXIOM_PORT="${AXIOM_PORT:-8888}"
AXIOM_MIN_FREE_KB="${AXIOM_MIN_FREE_KB:-10485760}"
AXIOM_COMPOSE_PROJECT="${AXIOM_COMPOSE_PROJECT:-}"
RELEASE_REF_WAS_SET="${AXIOM_RELEASE_REF+x}"
AXIOM_RELEASE_REF="${AXIOM_RELEASE_REF:-main}"
AXIOM_RELEASE_BASE_URL="${AXIOM_RELEASE_BASE_URL:-}"

log() { printf '[axiom-install] %s\n' "$*"; }
fail() { printf '[axiom-install] ERROR: %s\n' "$*" >&2; exit 1; }

on_error() {
  local code=$?
  printf '[axiom-install] ERROR: installation failed near line %s (exit %s)\n' "${BASH_LINENO[0]:-unknown}" "$code" >&2
  printf '[axiom-install] Inspect %s/.env and run: %s/axiomctl doctor\n' "$AXIOM_INSTALL_DIR" "$AXIOM_INSTALL_DIR" >&2
  exit "$code"
}
trap on_error ERR

OS="$(uname -s)"
ARCH="$(uname -m)"
if [[ "$OS" != Linux && "$OS" != Darwin ]]; then
  fail "only Linux and macOS are supported; detected $OS"
fi
if [[ "$OS" == Linux ]]; then
  case "$ARCH" in
    x86_64|amd64|aarch64|arm64) ;;
    *) fail "unsupported Linux architecture: $ARCH" ;;
  esac
fi

# 私有仓库：GH_TOKEN 同时用于 raw 资产下载与 ghcr.io 镜像拉取
# 代理隔离：raw.githubusercontent.com 通常被墙需走代理，但 Docker/Compose
# 的 ghcr.io 流量必须直连（企业代理会劫持证书）。代理只作用于本脚本的
# curl 下载（AXIOM_CURL_PROXY），并强制清除继承的全局代理变量。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
CURL_AUTH=()
CURL_PROXY_ARGS=()
if [[ -n "${GH_TOKEN:-}" ]]; then
  CURL_AUTH=(-H "Authorization: Bearer $GH_TOKEN")
fi
if [[ -n "${AXIOM_CURL_PROXY:-}" ]]; then
  CURL_PROXY_ARGS=(-x "$AXIOM_CURL_PROXY")
fi

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v docker >/dev/null 2>&1 || fail "Docker Engine is required; install Docker before running this installer"
docker compose version >/dev/null 2>&1 || fail "Docker Compose Plugin is required (run: docker compose version)"
docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable for the current user"

# 默认跟踪最新分支构建镜像（latest，随每次 push 自动更新）；
# 要安装固定 release：AXIOM_VERSION=0.2.0（部署物料与镜像同时锁定到该版本）
if [[ -z "$AXIOM_VERSION" && -z "${AXIOM_IMAGE+x}" ]]; then
  AXIOM_VERSION="latest"
fi

# Resolve the release metadata before downloading the remaining deployment assets.
# A custom base URL is useful for an audited mirror or an isolated test registry.
if [[ -z "$AXIOM_RELEASE_BASE_URL" ]]; then
  bootstrap_base="https://raw.githubusercontent.com/notstarr/AXIOM/${AXIOM_RELEASE_REF}/deploy"
  if [[ -z "$AXIOM_VERSION" ]]; then
    AXIOM_VERSION="$(curl --fail --location --silent --show-error --retry 3 --retry-delay 1 \
      ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} ${CURL_PROXY_ARGS[@]+"${CURL_PROXY_ARGS[@]}"} \
      "$bootstrap_base/VERSION" | tr -d '\r\n' || true)"
  fi
  if [[ -z "$RELEASE_REF_WAS_SET" && -n "$AXIOM_VERSION" && "$AXIOM_VERSION" != "latest" ]]; then
    AXIOM_RELEASE_REF="v$AXIOM_VERSION"
  fi
  AXIOM_RELEASE_BASE_URL="https://raw.githubusercontent.com/notstarr/AXIOM/${AXIOM_RELEASE_REF}/deploy"
elif [[ -z "$AXIOM_VERSION" ]]; then
  AXIOM_VERSION="$(curl --fail --location --silent --show-error --retry 3 --retry-delay 1 \
    "$AXIOM_RELEASE_BASE_URL/VERSION" | tr -d '\r\n' || true)"
fi
AXIOM_VERSION="${AXIOM_VERSION:-latest}"
AXIOM_IMAGE="${AXIOM_IMAGE:-ghcr.io/notstarr/axiom:${AXIOM_VERSION}}"
log "image: $AXIOM_IMAGE"
AXIOM_POSTGRES_IMAGE="${AXIOM_POSTGRES_IMAGE:-postgres:18.4-bookworm@sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382}"

# Keep the historical Compose project name for /opt/axiom, but make custom
# install directories safe to run side by side. Existing installations that
# predate this key continue to use Compose's default project name (`axiom`).
if [[ -z "$AXIOM_COMPOSE_PROJECT" ]]; then
  if [[ "$AXIOM_INSTALL_DIR" == /opt/axiom ]]; then
    AXIOM_COMPOSE_PROJECT=axiom
  else
    project_slug="$(basename -- "$AXIOM_INSTALL_DIR" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//')"
    AXIOM_COMPOSE_PROJECT="axiom-${project_slug:-instance}"
  fi
fi

prepare_install_dir() {
  local probe="$AXIOM_INSTALL_DIR/.axiom-write-test"
  if mkdir -p "$AXIOM_INSTALL_DIR" 2>/dev/null && touch "$probe" 2>/dev/null; then
    rm -f "$probe"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1; then
    log "$AXIOM_INSTALL_DIR is not writable; using sudo to prepare it"
    sudo mkdir -p "$AXIOM_INSTALL_DIR"
    sudo chown "$(id -u):$(id -g)" "$AXIOM_INSTALL_DIR"
    touch "$probe" || fail "cannot write $AXIOM_INSTALL_DIR after sudo preparation"
    rm -f "$probe"
    return 0
  fi
  fail "$AXIOM_INSTALL_DIR is not writable; set AXIOM_INSTALL_DIR to a writable directory or install sudo"
}
prepare_install_dir
mkdir -p "$AXIOM_INSTALL_DIR/logs" "$AXIOM_INSTALL_DIR/backups"
chmod 700 "$AXIOM_INSTALL_DIR/logs" "$AXIOM_INSTALL_DIR/backups"

available_kb="$(df -Pk "$AXIOM_INSTALL_DIR" | awk 'NR == 2 {print $4}')"
[[ "$available_kb" =~ ^[0-9]+$ ]] || fail "cannot determine free disk space for $AXIOM_INSTALL_DIR"
((available_kb >= AXIOM_MIN_FREE_KB)) || fail "less than $((AXIOM_MIN_FREE_KB / 1024)) MiB is available on the install filesystem; set AXIOM_MIN_FREE_KB to override"

port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn 2>/dev/null | awk -v port=":$AXIOM_PORT" '$4 ~ port "$" {found=1} END {exit !found}'
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -a -iTCP:"$AXIOM_PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}
if [[ ! -f "$AXIOM_INSTALL_DIR/.env" ]] && port_in_use; then
  fail "host TCP port $AXIOM_PORT is already in use; choose AXIOM_PORT or stop the conflicting service"
fi

download() {
  local path=$1 target="$AXIOM_INSTALL_DIR/$1"
  mkdir -p "$(dirname "$target")"
  log "downloading $path from $AXIOM_RELEASE_REF"
  curl --fail --location --silent --show-error --retry 3 --retry-delay 1 \
    ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} ${CURL_PROXY_ARGS[@]+"${CURL_PROXY_ARGS[@]}"} \
    "$AXIOM_RELEASE_BASE_URL/$path" -o "$target" \
    || fail "could not download $path from $AXIOM_RELEASE_BASE_URL"
}
for path in compose.yaml .env.example axiomctl postgres-init/001-axiom-roles.sh; do
  download "$path"
done
curl --fail --location --silent --show-error --retry 3 --retry-delay 1 \
  "$AXIOM_RELEASE_BASE_URL/VERSION" -o "$AXIOM_INSTALL_DIR/VERSION" \
  || printf '%s\n' "$AXIOM_VERSION" > "$AXIOM_INSTALL_DIR/VERSION"
chmod 755 "$AXIOM_INSTALL_DIR/axiomctl" "$AXIOM_INSTALL_DIR/postgres-init/001-axiom-roles.sh"

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}
set_env() {
  local key=$1 value=$2 file=$3
  if grep -qE "^${key}=" "$file"; then
    sed -i.bak -E "s|^${key}=.*$|${key}=${value}|" "$file"
    rm -f "$file.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}
ensure_env() {
  local key=$1 value=$2 file=$3 current
  current="$(grep -E "^${key}=" "$file" | head -n 1 | cut -d= -f2- || true)"
  if [[ -z "${current//[[:space:]]/}" ]]; then
    set_env "$key" "$value" "$file"
  fi
}
read_env_value() {
  local key=$1 file=$2 fallback=$3 value
  value="$(grep -E "^${key}=" "$file" | head -n 1 | cut -d= -f2- || true)"
  printf '%s' "${value:-$fallback}"
}

umask 077
if [[ ! -f "$AXIOM_INSTALL_DIR/.env" ]]; then
  cp "$AXIOM_INSTALL_DIR/.env.example" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_VERSION "$AXIOM_VERSION" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_COMPOSE_PROJECT "$AXIOM_COMPOSE_PROJECT" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_IMAGE "$AXIOM_IMAGE" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_PLATFORM "${AXIOM_PLATFORM:-linux/amd64}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_POSTGRES_IMAGE "$AXIOM_POSTGRES_IMAGE" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_PUBLISH_HOST "$AXIOM_PUBLISH_HOST" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_PORT "$AXIOM_PORT" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DATA_DIR "${AXIOM_DATA_DIR:-/var/lib/axiom}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DB_HOST "${AXIOM_DB_HOST:-postgres}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DB_PORT "${AXIOM_DB_PORT:-5432}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DB_NAME "${AXIOM_DB_NAME:-axiom}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_INTERNAL_SERVER_URL "${AXIOM_INTERNAL_SERVER_URL:-http://api:8899}" "$AXIOM_INSTALL_DIR/.env"
  set_env CHROME_HEADLESS "${CHROME_HEADLESS:-1}" "$AXIOM_INSTALL_DIR/.env"
  set_env CHROME_BIN "${CHROME_BIN:-/usr/bin/chromium}" "$AXIOM_INSTALL_DIR/.env"
  set_env CHROME_NO_SANDBOX "${CHROME_NO_SANDBOX:-1}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_CHROME_PROFILE_DIR "${AXIOM_CHROME_PROFILE_DIR:-/var/lib/axiom-browser-profile}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_POSTGRES_VOLUME "${AXIOM_POSTGRES_VOLUME:-axiom-postgres}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DATA_VOLUME "${AXIOM_DATA_VOLUME:-axiom-data}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_BROWSER_VOLUME "${AXIOM_BROWSER_VOLUME:-axiom-browser-profile}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_NETWORK "${AXIOM_NETWORK:-axiom}" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DB_MIGRATOR_PASSWORD "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_DB_APP_PASSWORD "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_POSTGRES_BOOTSTRAP_PASSWORD "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  set_env AXIOM_SERVICE_TOKEN "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
else
  ensure_env AXIOM_VERSION "$AXIOM_VERSION" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_IMAGE "$AXIOM_IMAGE" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_PLATFORM "${AXIOM_PLATFORM:-linux/amd64}" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_POSTGRES_IMAGE "$AXIOM_POSTGRES_IMAGE" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_PUBLISH_HOST "$AXIOM_PUBLISH_HOST" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_PORT "$AXIOM_PORT" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_DATA_DIR /var/lib/axiom "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_DB_HOST postgres "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_DB_PORT 5432 "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_DB_NAME axiom "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_INTERNAL_SERVER_URL http://api:8899 "$AXIOM_INSTALL_DIR/.env"
  ensure_env CHROME_HEADLESS 1 "$AXIOM_INSTALL_DIR/.env"
  ensure_env CHROME_BIN /usr/bin/chromium "$AXIOM_INSTALL_DIR/.env"
  ensure_env CHROME_NO_SANDBOX 1 "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_CHROME_PROFILE_DIR /var/lib/axiom-browser-profile "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_DB_MIGRATOR_PASSWORD "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_DB_APP_PASSWORD "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_POSTGRES_BOOTSTRAP_PASSWORD "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  ensure_env AXIOM_SERVICE_TOKEN "$(random_hex)" "$AXIOM_INSTALL_DIR/.env"
  log "keeping existing .env; existing credentials were not regenerated"
fi
for secret_key in AXIOM_DB_MIGRATOR_PASSWORD AXIOM_DB_APP_PASSWORD AXIOM_POSTGRES_BOOTSTRAP_PASSWORD AXIOM_SERVICE_TOKEN; do
  secret_value="$(grep -E "^${secret_key}=" "$AXIOM_INSTALL_DIR/.env" | head -n 1 | cut -d= -f2- || true)"
  [[ -n "${secret_value//[[:space:]]/}" && "$secret_value" != change-me-* ]] || fail "$secret_key is empty or still uses a change-me placeholder in $AXIOM_INSTALL_DIR/.env"
done
chmod 600 "$AXIOM_INSTALL_DIR/.env"

# On an idempotent rerun, use the persisted endpoint and image rather than
# shell defaults (for example, a custom AXIOM_PORT must remain the health target).
AXIOM_VERSION="$(read_env_value AXIOM_VERSION "$AXIOM_INSTALL_DIR/.env" "$AXIOM_VERSION")"
AXIOM_IMAGE="$(read_env_value AXIOM_IMAGE "$AXIOM_INSTALL_DIR/.env" "$AXIOM_IMAGE")"
AXIOM_PUBLISH_HOST="$(read_env_value AXIOM_PUBLISH_HOST "$AXIOM_INSTALL_DIR/.env" "$AXIOM_PUBLISH_HOST")"
AXIOM_PORT="$(read_env_value AXIOM_PORT "$AXIOM_INSTALL_DIR/.env" "$AXIOM_PORT")"

link_control_script() {
  local link=/usr/local/bin/axiomctl
  if [[ -e "$link" && ! -L "$link" ]]; then
    log "$link already exists and is not a symlink; use $AXIOM_INSTALL_DIR/axiomctl"
    return 0
  fi
  if ln -sfn "$AXIOM_INSTALL_DIR/axiomctl" "$link" 2>/dev/null; then
    log "installed axiomctl at $link"
  elif command -v sudo >/dev/null 2>&1 && sudo ln -sfn "$AXIOM_INSTALL_DIR/axiomctl" "$link"; then
    log "installed axiomctl at $link using sudo"
  else
    log "could not install $link; use $AXIOM_INSTALL_DIR/axiomctl"
  fi
}
link_control_script

if [[ -n "${GH_TOKEN:-}" ]]; then
  printf '%s' "$GH_TOKEN" | docker login ghcr.io -u "${GH_TOKEN_USER:-notstarr}" --password-stdin \
    || log "ghcr.io login failed; continuing (image may already be pulled)"
fi

log "pulling the pinned AXIOM image"
if ! (cd "$AXIOM_INSTALL_DIR" && docker compose --env-file .env -f compose.yaml pull postgres migrator api runner); then
  fail "image pull failed; check Docker registry access and run $AXIOM_INSTALL_DIR/axiomctl doctor"
fi
if ! "$AXIOM_INSTALL_DIR/axiomctl" pin-image; then
  fail "the AXIOM image was pulled but could not be resolved to an immutable digest"
fi
if ! "$AXIOM_INSTALL_DIR/axiomctl" start; then
  fail "AXIOM did not start; run $AXIOM_INSTALL_DIR/axiomctl doctor and logs"
fi

log "waiting for API health"
HEALTH_HOST="$AXIOM_PUBLISH_HOST"
[[ "$HEALTH_HOST" == 0.0.0.0 ]] && HEALTH_HOST=127.0.0.1
for _ in $(seq 1 60); do
  if curl --fail --silent --show-error --max-time 3 "http://${HEALTH_HOST}:${AXIOM_PORT}/health" | grep -q '"status":"ok"'; then
    printf '\nAXIOM is ready.\n  URL:       http://%s:%s\n  Directory: %s\n  Control:   %s/axiomctl\n\n' "$HEALTH_HOST" "$AXIOM_PORT" "$AXIOM_INSTALL_DIR" "$AXIOM_INSTALL_DIR"
    exit 0
  fi
  sleep 2
done
fail "API did not become healthy; run $AXIOM_INSTALL_DIR/axiomctl doctor and logs"
