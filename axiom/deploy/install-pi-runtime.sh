#!/usr/bin/env bash
# AXIOM Pi Worker 运行时引导（幂等，可重复执行）。
#
# 作用：runtime/ 整目录被 .gitignore，从 GitHub 干净 clone 后 Worker 没有
# pi 二进制。本脚本按 src/axiom/modules/workers/pi_config.py 的版本常量
# （唯一真相源）安装完全一致的 Pi 运行时：
#   1. runtime/node/<ver>/     —— 官方 Node.js 发行版（pi 的执行时）
#   2. runtime/pi/<ver>/       —— npm 包 @earendil-works/pi-coding-agent@<ver>
#   3. runtime/pi/<ver>/bin/pi —— 启动 wrapper（与生产机逐字节同构）
#   4. 版本校验：bin/pi --version 必须等于常量声明
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO}"

# ── 版本常量：以 pi_config.py 为唯一真相源（grep 直读，不依赖 venv）──
PI_CONFIG="src/axiom/modules/workers/pi_config.py"
PI_VERSION="$(sed -n 's/^PI_RUNTIME_VERSION = "\(.*\)"$/\1/p' "${PI_CONFIG}" | head -1)"
NODE_VERSION="$(sed -n 's/^PI_NODE_VERSION = "\(.*\)"$/\1/p' "${PI_CONFIG}" | head -1)"
if [[ -z "${PI_VERSION}" || -z "${NODE_VERSION}" ]]; then
  echo "[pi-runtime] ERROR: cannot parse versions from ${PI_CONFIG}" >&2
  exit 1
fi
echo "[pi-runtime] target: pi ${PI_VERSION} / node ${NODE_VERSION} (from pi_config.py)"

PI_DIR="${REPO}/runtime/pi/${PI_VERSION}"
NODE_VER_SHORT="${NODE_VERSION#v}"
NODE_DIR="${REPO}/runtime/node/${NODE_VER_SHORT}"
NODE_DIR_V="${REPO}/runtime/node/${NODE_VERSION}"
# Pi's floating ^1.x siblings are not reproducible: 1.3.0 renamed an export
# that 1.1.0 imports, and Linux loads the codemode extension while macOS does
# not.  Use the repository lockfile, which pins the tested 1.1.0 dependency set.
PI_PACKAGE_JSON="${REPO}/deploy/pi-runtime/package.json"
PI_PACKAGE_LOCK="${REPO}/deploy/pi-runtime/package-lock.json"

# ── 已装好则直接校验退出 ────────────────────────────────────────────
if [[ -x "${PI_DIR}/bin/pi" ]]; then
  INSTALLED="$("${PI_DIR}/bin/pi" --version 2>/dev/null || true)"
  if [[ "${INSTALLED}" == "${PI_VERSION}" ]]; then
    echo "[pi-runtime] OK: already installed and version matches: ${PI_DIR}/bin/pi (${INSTALLED})"
    exit 0
  fi
  echo "[pi-runtime] WARN: existing version mismatch (${INSTALLED} != ${PI_VERSION}), reinstalling" >&2
  rm -rf "${PI_DIR}"
fi

# ── 1. Node.js 运行时 ───────────────────────────────────────────────
if [[ ! -x "${NODE_DIR}/bin/node" ]]; then
  echo "[pi-runtime] downloading Node.js ${NODE_VERSION} ..."
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) NODE_ARCH="darwin-arm64" ;;
    Darwin-x86_64) NODE_ARCH="darwin-x64" ;;
    Linux-x86_64) NODE_ARCH="linux-x64" ;;
    Linux-aarch64) NODE_ARCH="linux-arm64" ;;
    *) echo "[pi-runtime] ERROR: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
  esac
  TARBALL="node-${NODE_VERSION}-${NODE_ARCH}.tar.gz"
  URL="https://nodejs.org/dist/${NODE_VERSION}/${TARBALL}"
  TMP="$(mktemp -d)"
  if ! curl -fSL --retry 3 -o "${TMP}/${TARBALL}" "${URL}"; then
    echo "[pi-runtime] nodejs.org direct failed, retrying via local proxy 127.0.0.1:7890 ..." >&2
    curl -fSL --retry 3 -o "${TMP}/${TARBALL}" -x http://127.0.0.1:7890 "${URL}"
  fi
  mkdir -p "${REPO}/runtime/node"
  tar -xzf "${TMP}/${TARBALL}" -C "${REPO}/runtime/node"
  mv "${REPO}/runtime/node/node-${NODE_VERSION}-${NODE_ARCH}" "${NODE_DIR}"
  rm -rf "${TMP}"
fi
"${NODE_DIR}/bin/node" --version

# wrapper 与 pi_config 双路径兼容：pi_config 找 <ver>，wrapper 找 v<ver>
if [[ ! -e "${NODE_DIR_V}" ]]; then
  ln -sfn "${NODE_VER_SHORT}" "${NODE_DIR_V}"
fi

# ── 2. npm 安装 pi-coding-agent（仓库 lockfile，锁定已知可用依赖集）────
NPM="${NODE_DIR}/bin/npm"
[[ -x "${NPM}" ]] || { echo "[pi-runtime] ERROR: npm not found in Node dist" >&2; exit 1; }
[[ -f "${PI_PACKAGE_JSON}" && -f "${PI_PACKAGE_LOCK}" ]] \
  || { echo "[pi-runtime] ERROR: deploy/pi-runtime lockfiles are missing" >&2; exit 1; }
if [[ ! -f "${PI_DIR}/node_modules/@earendil-works/pi-coding-agent/package.json" ]]; then
  echo "[pi-runtime] npm ci from deploy/pi-runtime/package-lock.json ..."
  mkdir -p "${PI_DIR}"
  cp "${PI_PACKAGE_JSON}" "${PI_DIR}/package.json"
  cp "${PI_PACKAGE_LOCK}" "${PI_DIR}/package-lock.json"
  (cd "${PI_DIR}" && "${NPM}" ci --omit=dev --no-audit --no-fund)
fi

# pi-web-access is intentionally not part of the lockfile: installing it can
# hoist a newer @earendil-works/pi-codemode and break Pi on Linux.  Add web
# search through an MCP server or a separately pinned extension if needed.

# ── 3. bin/pi wrapper（与生产机同构）────────────────────────────────
mkdir -p "${PI_DIR}/bin"
cat > "${PI_DIR}/bin/pi" <<EOF
#!/bin/sh
set -eu
HERE=\$(CDPATH= cd -- "\$(dirname -- "\$0")" && pwd)
exec "\$HERE/../../../node/${NODE_VERSION}/bin/node" "\$HERE/../node_modules/@earendil-works/pi-coding-agent/dist/cli.js" "\$@"
EOF
chmod 0755 "${PI_DIR}/bin/pi"

# ── 4. 版本校验 ─────────────────────────────────────────────────────
INSTALLED="$("${PI_DIR}/bin/pi" --version 2>/dev/null || true)"
if [[ "${INSTALLED}" != "${PI_VERSION}" ]]; then
  echo "[pi-runtime] ERROR: version check failed: installed ${INSTALLED} != expected ${PI_VERSION}" >&2
  exit 1
fi
echo "[pi-runtime] OK: Pi ${INSTALLED} ready: ${PI_DIR}/bin/pi"
