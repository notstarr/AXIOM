#!/usr/bin/env bash
# AXIOM Worker 安全工具链引导（幂等）：安装 Worker 会话里 skills 实际调用的外部二进制。
#
# 版本对齐基准：生产 Mac 实测版本，记录在 deploy/tools-versions.env。
# 下载一律走 GitHub API 资产流（api.github.com），绕开被墙的 release CDN。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="${AXIOM_TOOLS_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "${BIN_DIR}"
cd "${REPO}"

# ── 版本清单 ────────────────────────────────────────────────────────
if [[ -f "$(dirname "$0")/tools-versions.env" ]]; then
  # shellcheck disable=SC1091
  source "$(dirname "$0")/tools-versions.env"
else
  echo "[tools] ERROR: deploy/tools-versions.env not found" >&2
  exit 1
fi

have() { command -v "$1" >/dev/null 2>&1 || [[ -x "${BIN_DIR}/$1" ]]; }

# ── 平台关键词（各仓库资产命名大小写不一，统一小写包含匹配）─────────
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  PD_OS="macos_arm64";  FFUF_OS="darwin_arm64" ;;
  Darwin-x86_64) PD_OS="macos_amd64";  FFUF_OS="darwin_amd64" ;;
  Linux-x86_64)  PD_OS="linux_amd64";  FFUF_OS="linux_amd64" ;;
  Linux-aarch64) PD_OS="linux_arm64";  FFUF_OS="linux_arm64" ;;
  *) echo "[tools] ERROR: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

# ── GitHub release 资产下载（API 资产流 + 关键词模糊匹配）───────────
gh_pick_asset_id() {
  # gh_pick_asset_id <repo> <tag> <kw...>  → 资产 ID（ stdout），失败输出空
  local repo="$1" tag="$2"; shift 2
  local release_json
  release_json="$(curl -fsSL --retry 3 "https://api.github.com/repos/${repo}/releases/${tag}" 2>/dev/null || true)"
  if [[ -z "${release_json}" ]]; then
    echo "[tools]   WARN: ${repo}@${tag} release info unavailable (network/rate limit)" >&2
    return 1
  fi
  printf '%s' "${release_json}" | python3 - "$@" <<'PY'
import json, sys
release = json.load(sys.stdin)
kws = [k.lower() for k in sys.argv[1:]]
for a in release.get("assets", []):
    name = a["name"].lower()
    if all(k in name for k in kws):
        print(a["id"])
        break
PY
}

# install_from_release <tool> <repo> <tag> <dest-bin> <kw...>
#   下载 → zip 自动解压找 <tool> 二进制 → 否则视下载体本身为二进制
install_from_release() {
  local tool="$1" repo="$2" tag="$3" dest="$4"; shift 4
  if have "${tool}"; then
    echo "[tools] OK: ${tool} already present, skip"
    return 0
  fi
  echo "[tools] install ${tool} ${tag} (${repo}) ..."
  local asset_id
  asset_id="$(gh_pick_asset_id "${repo}" "${tag}" "$@")" || return 1
  if [[ -z "${asset_id}" ]]; then
    echo "[tools]   WARN: no matching asset on ${repo}@${tag} for keywords: $*" >&2
    return 1
  fi
  local dl="${BIN_DIR}/.${tool}.dl"
  curl -fsSL --retry 3 -H "Accept: application/octet-stream" -o "${dl}" \
    "https://api.github.com/repos/${repo}/releases/assets/${asset_id}" \
    || { rm -f "${dl}"; echo "[tools]   WARN: download failed: ${tool}" >&2; return 1; }
  # zip 魔数 PK → 解压找二进制；否则下载体本身即二进制
  if [[ "$(head -c 2 "${dl}" 2>/dev/null)" == "PK" ]]; then
    local tmp
    tmp="$(mktemp -d)"
    if command -v unzip >/dev/null 2>&1; then
      unzip -o -q "${dl}" -d "${tmp}"
    else
      python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "${dl}" "${tmp}"
    fi
    local inner
    inner="$(find "${tmp}" -type f -name "${tool}" | head -1)"
    if [[ -z "${inner}" ]]; then
      inner="$(find "${tmp}" -type f -perm +111 | head -1)"
    fi
    [[ -n "${inner}" ]] || { echo "[tools]   WARN: ${tool} not found inside archive" >&2; rm -rf "${tmp}" "${dl}"; return 1; }
    mv "${inner}" "${dest}"
    rm -rf "${tmp}"
  else
    mv "${dl}" "${dest}"
  fi
  chmod +x "${dest}"
  rm -f "${dl}"
  echo "[tools] OK: ${tool} -> ${dest}"
}

# ── 1. projectdiscovery 五件套 ──────────────────────────────────────
install_from_release subfinder "projectdiscovery/subfinder" "${SUBFINDER_VERSION}" "${BIN_DIR}/subfinder" "subfinder" "${PD_OS}" "zip"
install_from_release httpx     "projectdiscovery/httpx"     "${HTTPX_VERSION}"     "${BIN_DIR}/httpx"     "httpx"     "${PD_OS}" "zip"
install_from_release nuclei    "projectdiscovery/nuclei"    "${NUCLEI_VERSION}"    "${BIN_DIR}/nuclei"    "nuclei"    "${PD_OS}" "zip"
install_from_release katana    "projectdiscovery/katana"    "${KATANA_VERSION}"    "${BIN_DIR}/katana"    "katana"    "${PD_OS}" "zip"
install_from_release naabu     "projectdiscovery/naabu"     "${NAABU_VERSION}"     "${BIN_DIR}/naabu"     "naabu"     "${PD_OS}" "zip"

# ── 2. ffuf ─────────────────────────────────────────────────────────
install_from_release ffuf "joohoi/ffuf" "${FFUF_VERSION}" "${BIN_DIR}/ffuf" "ffuf" "${FFUF_OS}" "tar.gz"

# ── 3. nmap（二进制 + 数据目录；macOS 走 brew，Linux 走系统包）──────
NMAP_DATA="${AXIOM_NMAP_DATA_DIR:-$HOME/.local/share/nmap}"
if have nmap && [[ -f "${NMAP_DATA}/nmap-services" ]]; then
  echo "[tools] OK: nmap already present and data dir complete, skip"
else
  echo "[tools] install nmap ${NMAP_VERSION} ..."
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    brew install nmap
    mkdir -p "${NMAP_DATA}"
    NMAP_PREFIX="$(brew --prefix nmap)"
    cp -f "${NMAP_PREFIX}/share/nmap/nmap-services" "${NMAP_DATA}/" 2>/dev/null || true
    cp -f "${NMAP_PREFIX}/share/nmap/nmap-protocols" "${NMAP_DATA}/" 2>/dev/null || true
    cp -f "${NMAP_PREFIX}/share/nmap/nse_main.lua" "${NMAP_DATA}/" 2>/dev/null || true
    cp -Rf "${NMAP_PREFIX}/share/nmap/scripts" "${NMAP_DATA}/" 2>/dev/null || true
    ln -sf "$(command -v nmap)" "${BIN_DIR}/nmap"
  elif [[ "$(uname -s)" == "Linux" ]] && command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y nmap
  else
    echo "[tools] WARN: nmap needs manual install (brew install nmap / apt install nmap)" >&2
  fi
  echo "[tools] OK: nmap (data dir ${NMAP_DATA}, validated by start.sh)"
fi

# ── 4. sqlmap（pipx / pip --user / git clone 三选一）────────────────
if have sqlmap; then
  echo "[tools] OK: sqlmap already present, skip"
else
  echo "[tools] install sqlmap ${SQLMAP_VERSION} ..."
  if command -v pipx >/dev/null 2>&1; then
    pipx install "sqlmap==${SQLMAP_VERSION}"
  elif command -v pip3 >/dev/null 2>&1; then
    pip3 install --user "sqlmap==${SQLMAP_VERSION}" \
      && ln -sf "$HOME/.local/bin/sqlmap" "${BIN_DIR}/sqlmap" 2>/dev/null || true
  else
    git clone --depth 1 https://github.com/sqlmapproject/sqlmap.git "$HOME/.local/share/sqlmap" \
      && printf '#!/bin/sh\nexec %s "$HOME/.local/share/sqlmap/sqlmap.py" "$@"\n' "$(command -v python3)" > "${BIN_DIR}/sqlmap" \
      && chmod +x "${BIN_DIR}/sqlmap"
  fi
  echo "[tools] OK: sqlmap"
fi

# ── 5. fscan（内网综合扫描；官方不发 macOS 包，mac 需自建或用已有二进制）──
install_from_release fscan "shadow1ng/fscan" "${FSCAN_VERSION}" "${BIN_DIR}/fscan" "fscan_" "${PD_OS}"

echo
echo "[tools] checklist:"
for t in nmap subfinder httpx nuclei katana ffuf naabu sqlmap fscan; do
  have "$t" && echo "  OK   $t" || echo "  MISS $t (see log above)"
done
echo
echo "[tools] note: make sure ${BIN_DIR} is on the Runner PATH (add to launchd PATH"
echo "[tools]       or symlink binaries into /usr/local/bin)."
