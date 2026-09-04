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

# 浏览器 broker 通过 CHROME_BIN/PATH 启动 Chromium
RUN ln -s "$(find /ms-playwright -maxdepth 3 -path '*chromium*/chrome-linux*/chrome' | head -n 1)" /usr/local/bin/chrome \
 && chmod 0755 /usr/local/bin/chrome

ENV CHROME_BIN=/usr/local/bin/chrome \
    AXIOM_DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"

VOLUME ["/data"]
EXPOSE 8888
