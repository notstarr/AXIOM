# syntax=docker/dockerfile:1
# AXIOM 一体化镜像：API/控制台 + 调度 Runner + pi Worker 运行时 + Chromium
# 国内网络可用 build-args 切镜像源（见 compose.deploy.yaml 注释），默认官方源
# 平台钉死 amd64：vendored runtime/node + runtime/pi 为 linux x86_64 构建
ARG BASE_IMAGE=python:3.12-slim
ARG NODE_VERSION=22.22.2
ARG NODE_DIST_MIRROR=https://nodejs.org/dist
FROM --platform=linux/amd64 ${BASE_IMAGE}
ARG NODE_VERSION
ARG NODE_DIST_MIRROR

ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ARG PLAYWRIGHT_DOWNLOAD_HOST=

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    CHROME_HEADLESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN set -eux; \
    if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
      sed -ri "s|http://deb.debian.org|http://${APT_MIRROR}|g; s|https://deb.debian.org|https://${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git xz-utils \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "uv==0.8.9"

WORKDIR /app

# 依赖层（锁文件不变则命中缓存）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 代码 + vendored 运行时（runtime/node + runtime/pi 为 linux x86_64 构建，容器内原生可用）
COPY . .
# 仓库 vendored 的 runtime/node 可能是构建机本地产物（macOS 二进制），
# 镜像内强制替换为官方 linux-x64 发行版，保证 pi Worker 可执行
RUN set -eux; \
    cd /tmp \
 && curl -fsSLO "${NODE_DIST_MIRROR}/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && rm -rf /app/runtime/node/v${NODE_VERSION} \
 && mkdir -p /app/runtime/node \
 && tar -xJf node-v${NODE_VERSION}-linux-x64.tar.xz -C /app/runtime/node \
 && mv /app/runtime/node/node-v${NODE_VERSION}-linux-x64 /app/runtime/node/v${NODE_VERSION} \
 && rm -f node-v${NODE_VERSION}-linux-x64.tar.xz \
 && ln -sf /app/runtime/node/v${NODE_VERSION}/bin/node /usr/local/bin/node \
 && /usr/local/bin/node --version
RUN uv sync --frozen --no-dev \
 && if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then export PLAYWRIGHT_DOWNLOAD_HOST; fi \
 && uv run --no-sync playwright install --with-deps chromium

# Pi Worker 运行时：runtime/pi 被 gitignore，从 GitHub 干净 clone 构建时
# 本目录为空。这里用与本机一致的锁文件（deploy/pi-runtime/package-lock.json，
# 钉死 pi-coding-agent 与 pi-codemode 的兼容版本组合）执行 npm ci 并生成
# 与生产机同构的 bin/pi wrapper（本机构建若已 vendored 则自动跳过）。
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

# Worker 安全工具链：版本与生产 Mac 实测对齐（2026-10-14 抓取）。
# GitHub Actions 构建机上直接下载 release 资产不受本地网络限制。
ARG SUBFINDER_VERSION=v2.14.0
ARG HTTPX_VERSION=v1.9.0
ARG NUCLEI_VERSION=v3.8.0
ARG KATANA_VERSION=v1.6.1
ARG NAABU_VERSION=v2.6.1
ARG FFUF_VERSION=2.1.0
ARG SQLMAP_VERSION=1.10.5.3
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
    dl joohoi/ffuf "${FFUF_VERSION}" "ffuf_${FFUF_VERSION}_linux_amd64.tar.gz" \
 && tar -xzf /tmp/tool.dl -C /tmp && mv /tmp/ffuf /usr/local/bin/ffuf && rm -f /tmp/tool.dl; \
    git clone --depth 1 --branch "${SQLMAP_VERSION}" https://github.com/sqlmapproject/sqlmap.git /opt/sqlmap; \
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
