# syntax=docker/dockerfile:1
# AXIOM 一体化镜像：API/控制台 + 调度 Runner + pi Worker 运行时 + Chromium + 安全工具链
# 国内网络可用 build-args 切镜像源（见 compose.deploy.yaml 注释），默认官方源
# 架构自适应：Mac Apple Silicon 构建 arm64 原生镜像，CI/服务器构建 amd64

# ── 全局 ARG（必须在第一个 FROM 之前声明，对所有 stage 生效）────────
ARG BASE_IMAGE=python:3.12-slim
ARG NODE_VERSION=22.22.2
ARG NODE_DIST_MIRROR=https://nodejs.org/dist
ARG GOLANG_IMAGE=golang:1.24-bookworm
ARG FFUF_VERSION=2.1.0

# ── ffuf 构建阶段：官方 GitHub releases/tags 已清空，改走 Go module
#    proxy 安装固定版本（proxy 缓存历史版本，不依赖 GitHub tags 存续）──
# GOPROXY=direct + GOSUMDB=off：本地 Docker 构建（含企业代理/证书劫持环境）
# 直接从 GitHub 取源码；CI 上 GitHub 同样可达。pinned 版本保证可复现。
FROM ${GOLANG_IMAGE} AS ffuf-builder
ARG FFUF_VERSION
# 清空 Docker Desktop 注入的代理变量：容器直连 GitHub（工具层已验证直连可达），
# 避免代理链上的证书劫持破坏 go 模块下载；CI 环境无代理，行为不变。
ENV HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy= NO_PROXY=* \
    GOPROXY=direct \
    GOSUMDB=off \
    GIT_TERMINAL_PROMPT=0
RUN CGO_ENABLED=0 go install -trimpath -ldflags="-s -w" \
      github.com/ffuf/ffuf/v2@v${FFUF_VERSION}

# ── 主阶段：API/控制台 + Runner + pi + Chromium + 工具链 ────────────
FROM ${BASE_IMAGE}
ARG NODE_VERSION
ARG NODE_DIST_MIRROR
ARG SUBFINDER_VERSION=v2.14.0
ARG HTTPX_VERSION=v1.9.0
ARG NUCLEI_VERSION=v3.8.0
ARG KATANA_VERSION=v1.6.1
ARG NAABU_VERSION=v2.6.1

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_DEFAULT_INDEX=https://pypi.org/simple \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    CHROME_HEADLESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN set -eux; \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git xz-utils \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "uv==0.8.9"

WORKDIR /app

# 依赖层（锁文件不变则命中缓存）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 代码 + vendored 运行时（runtime/node 为 linux x86_64，镜像内强制替换官方发行版）
COPY . .
RUN set -eux; \
    case "$(uname -m)" in x86_64) NDIST=linux-x64 ;; aarch64) NDIST=linux-arm64 ;; *) NDIST=linux-x64 ;; esac; \
    cd /tmp \
 && curl -fsSLO "${NODE_DIST_MIRROR}/v${NODE_VERSION}/node-v${NODE_VERSION}-${NDIST}.tar.xz" \
 && rm -rf /app/runtime/node/v${NODE_VERSION} \
 && mkdir -p /app/runtime/node \
 && tar -xJf node-v${NODE_VERSION}-${NDIST}.tar.xz -C /app/runtime/node \
 && mv /app/runtime/node/node-v${NODE_VERSION}-${NDIST} /app/runtime/node/v${NODE_VERSION} \
 && rm -f node-v${NODE_VERSION}-${NDIST}.tar.xz \
 && ln -sf /app/runtime/node/v${NODE_VERSION}/bin/node /usr/local/bin/node \
 && /usr/local/bin/node --version
RUN uv sync --frozen --no-dev \
 && uv run --no-sync playwright install --with-deps chromium

# Pi Worker 运行时：runtime/pi 被 gitignore，干净 clone 构建时用与本机
# 一致的锁文件（deploy/pi-runtime/package-lock.json，钉死 pi-coding-agent
# 与 pi-codemode 兼容组合）执行 npm ci（本机构建若已 vendored 则自动跳过）。
COPY deploy/pi-runtime/package.json deploy/pi-runtime/package-lock.json /tmp/pi-runtime/
RUN set -eux; \
    PI_VERSION=$(.venv/bin/python -c "from axiom.modules.workers.pi_config import PI_RUNTIME_VERSION as v; print(v)"); \
    PI_DIR=/app/runtime/pi/${PI_VERSION}; \
    PKG_JSON="$PI_DIR/node_modules/@earendil-works/pi-coding-agent/package.json"; \
    if [ ! -f "$PKG_JSON" ]; then \
      mkdir -p "$PI_DIR/bin"; \
      cp /tmp/pi-runtime/package.json /tmp/pi-runtime/package-lock.json "$PI_DIR/"; \
      (cd "$PI_DIR" && /app/runtime/node/v${NODE_VERSION}/bin/npm ci --omit=dev --no-audit --no-fund); \
      printf '#!/bin/sh\nset -eu\nHERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$HERE/../../../node/v%s/bin/node" "$HERE/../node_modules/@earendil-works/pi-coding-agent/dist/cli.js" "$@"\n' "${NODE_VERSION}" > "$PI_DIR/bin/pi"; \
      chmod 0755 "$PI_DIR/bin/pi"; \
      ln -sfn "v${NODE_VERSION}" /app/runtime/node/${NODE_VERSION}; \
    fi; \
    "$PI_DIR/bin/pi" --version

# ffuf：从 Go builder 阶段拷入
COPY --from=ffuf-builder /go/bin/ffuf /usr/local/bin/ffuf

# Worker 安全工具链：版本与生产 Mac 实测对齐（2026-10-14 抓取）。
# GitHub Actions 构建机上直接下载 release 资产不受本地网络限制。
RUN set -eux; \
    apt-get update \
 && apt-get install -y --no-install-recommends nmap unzip \
 && rm -rf /var/lib/apt/lists/*; \
    dl() { curl -fsSL --retry 3 -o /tmp/tool.dl "https://github.com/$1/releases/download/$2/$3"; }; \
    unzip_tool() { local d; d="$(mktemp -d)"; unzip -o -q /tmp/tool.dl -d "$d"; \
      find "$d" -type f -name "$1" -exec mv {} "$2" \; ; chmod 0755 "$2"; rm -rf "$d" /tmp/tool.dl; }; \
    dl projectdiscovery/subfinder "${SUBFINDER_VERSION}" "subfinder_${SUBFINDER_VERSION#v}_linux_amd64.zip" \
 && unzip_tool subfinder /usr/local/bin/subfinder; \
    dl projectdiscovery/httpx "${HTTPX_VERSION}" "httpx_${HTTPX_VERSION#v}_linux_amd64.zip" \
 && unzip_tool httpx /usr/local/bin/httpx; \
    dl projectdiscovery/nuclei "${NUCLEI_VERSION}" "nuclei_${NUCLEI_VERSION#v}_linux_amd64.zip" \
 && unzip_tool nuclei /usr/local/bin/nuclei; \
    dl projectdiscovery/katana "${KATANA_VERSION}" "katana_${KATANA_VERSION#v}_linux_amd64.zip" \
 && unzip_tool katana /usr/local/bin/katana; \
    dl projectdiscovery/naabu "${NAABU_VERSION}" "naabu_${NAABU_VERSION#v}_linux_amd64.zip" \
 && unzip_tool naabu /usr/local/bin/naabu; \
    git clone --depth 1 https://github.com/sqlmapproject/sqlmap.git /opt/sqlmap; \
    printf '#!/bin/sh\nexec /app/.venv/bin/python /opt/sqlmap/sqlmap.py "$@"\n' > /usr/local/bin/sqlmap; \
    chmod 0755 /usr/local/bin/sqlmap; \
    nmap --version | head -2; subfinder -version 2>&1 | head -1; nuclei -version 2>&1 | head -1

# 浏览器 broker 通过 CHROME_BIN/PATH 启动 Chromium
RUN ln -s "$(find /ms-playwright -maxdepth 3 -path '*chromium*/chrome-linux*/chrome' | head -n 1)" /usr/local/bin/chrome \
 && chmod 0755 /usr/local/bin/chrome

ENV CHROME_BIN=/usr/local/bin/chrome \
    AXIOM_DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME ["/data"]
EXPOSE 8888
