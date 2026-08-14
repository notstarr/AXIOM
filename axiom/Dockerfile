# syntax=docker/dockerfile:1.7

# Release builds must pass immutable @sha256 references for all bases. Keeping
# these as required args prevents a local tag from silently becoming a release
# dependency.
ARG AXIOM_NODE_BASE_IMAGE
ARG AXIOM_GOLANG_BASE_IMAGE
ARG AXIOM_PYTHON_BASE_IMAGE

FROM ${AXIOM_NODE_BASE_IMAGE} AS frontend-builder
WORKDIR /build/axiom
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY tailwind.config.js ./
COPY src/axiom/static ./src/axiom/static
RUN npm run build:css

FROM ${AXIOM_GOLANG_BASE_IMAGE} AS tools-builder
ARG AXIOM_FFUF_VERSION=v2.1.0
ARG AXIOM_KATANA_VERSION=v1.1.3
ARG AXIOM_HTTPX_VERSION=v1.6.10
ARG AXIOM_SUBFINDER_VERSION=v2.6.7
ARG AXIOM_NUCLEI_VERSION=v3.3.7
ARG AXIOM_GOPROXY=https://proxy.golang.org,direct
# katana v1.1.3 uses go-tree-sitter, which requires CGO. Build against the
# same Debian/glibc family as the final runtime so the copied binaries remain
# executable without adding a second C library to the production image.
ENV CGO_ENABLED=1 GOPROXY=${AXIOM_GOPROXY}
# Official Golang images already provide this PATH; keep it explicit for Linux build/test bases too.
ENV PATH="/usr/local/go/bin:/go/bin:${PATH}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    go install "github.com/ffuf/ffuf/v2@${AXIOM_FFUF_VERSION}"; \
    go install "github.com/projectdiscovery/katana/cmd/katana@${AXIOM_KATANA_VERSION}"; \
    go install "github.com/projectdiscovery/httpx/cmd/httpx@${AXIOM_HTTPX_VERSION}"; \
    go install "github.com/projectdiscovery/subfinder/v2/cmd/subfinder@${AXIOM_SUBFINDER_VERSION}"; \
    go install "github.com/projectdiscovery/nuclei/v3/cmd/nuclei@${AXIOM_NUCLEI_VERSION}"

FROM ${AXIOM_PYTHON_BASE_IMAGE} AS runtime-validator
WORKDIR /validate
COPY runtime/ /opt/axiom/runtime/
COPY deploy/verify_runtime.py /app/deploy/verify_runtime.py
RUN chmod +x /app/deploy/verify_runtime.py \
    && AXIOM_RUNTIME_ROOT=/opt/axiom/runtime python /app/deploy/verify_runtime.py

FROM ${AXIOM_PYTHON_BASE_IMAGE} AS python-builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build/axiom
RUN pip install --no-cache-dir uv==0.8.9
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev --no-editable
COPY --from=frontend-builder /build/axiom/src/axiom/static/tailwind.generated.css ./src/axiom/static/tailwind.generated.css

FROM ${AXIOM_PYTHON_BASE_IMAGE} AS runtime
ARG AXIOM_UID=10001
ARG AXIOM_GID=10001
ARG AXIOM_VERSION=0.1.0
ARG AXIOM_REVISION=unknown
ARG AXIOM_BUILD_DATE=unknown
ARG AXIOM_DATABASE_MIGRATION_TARGET=postgresql@head
ARG AXIOM_DATABASE_ROLLBACK_COMPATIBLE=0
ENV PATH="/opt/axiom/runtime/node/v22.22.2/bin:/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    VIRTUAL_ENV="/opt/venv" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/axiom/src \
    AXIOM_RUNTIME_ROOT=/opt/axiom/runtime \
    AXIOM_DATA_DIR=/var/lib/axiom \
    AXIOM_CHROME_PROFILE_DIR=/var/lib/axiom-browser-profile \
    CHROME_HEADLESS=1 \
    CHROME_BIN=/usr/bin/chromium \
    CHROME_NO_SANDBOX=1 \
    AXIOM_BUILD_VERSION=${AXIOM_VERSION} \
    AXIOM_BUILD_REVISION=${AXIOM_REVISION} \
    AXIOM_BUILD_DATE=${AXIOM_BUILD_DATE} \
    AXIOM_DATABASE_MIGRATION_TARGET=${AXIOM_DATABASE_MIGRATION_TARGET} \
    AXIOM_DATABASE_ROLLBACK_COMPATIBLE=${AXIOM_DATABASE_ROLLBACK_COMPATIBLE}
LABEL org.opencontainers.image.title="AXIOM" \
      org.opencontainers.image.source="https://github.com/notstarr/AXIOM" \
      org.opencontainers.image.version="${AXIOM_VERSION}" \
      org.opencontainers.image.revision="${AXIOM_REVISION}" \
      org.opencontainers.image.created="${AXIOM_BUILD_DATE}" \
      io.axiom.database.migration-target="${AXIOM_DATABASE_MIGRATION_TARGET}" \
      io.axiom.database.rollback-compatible="${AXIOM_DATABASE_ROLLBACK_COMPATIBLE}"
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates chromium curl jq nmap passwd procps tini sqlmap \
    && rm -rf /var/lib/apt/lists/*
RUN /usr/sbin/groupadd --gid "${AXIOM_GID}" axiom \
    && /usr/sbin/useradd --uid "${AXIOM_UID}" --gid "${AXIOM_GID}" --create-home --shell /usr/sbin/nologin axiom
ENV PATH="/opt/axiom/runtime/node/v22.22.2/bin:/usr/local/bin:/opt/venv/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COPY --from=python-builder /opt/venv /opt/venv
COPY --from=tools-builder /go/bin/ffuf /usr/local/bin/ffuf
COPY --from=tools-builder /go/bin/katana /usr/local/bin/katana
COPY --from=tools-builder /go/bin/httpx /usr/local/bin/httpx
COPY --from=tools-builder /go/bin/subfinder /usr/local/bin/subfinder
COPY --from=tools-builder /go/bin/nuclei /usr/local/bin/nuclei
COPY src/axiom /app/axiom/src/axiom
COPY migrations /app/axiom/migrations
COPY alembic.ini pyproject.toml /app/axiom/
COPY --from=frontend-builder /build/axiom/src/axiom/static/tailwind.generated.css /app/axiom/src/axiom/static/tailwind.generated.css
# Preserve the checked-in artifact used by the current 8888 source after validating the CSS build above.
COPY src/axiom/static/tailwind.generated.css /app/axiom/src/axiom/static/tailwind.generated.css
COPY deploy/verify_runtime.py deploy/verify_toolchain.sh deploy/smoke_runtime.py deploy/generate_release_manifest.py /app/deploy/
COPY --from=runtime-validator /opt/axiom/runtime /opt/axiom/runtime
RUN chmod +x /app/deploy/verify_runtime.py /app/deploy/verify_toolchain.sh /app/deploy/smoke_runtime.py /app/deploy/generate_release_manifest.py \
    && test -f /opt/axiom/runtime/manifest.json \
    && AXIOM_RUNTIME_ROOT=/opt/axiom/runtime python /app/deploy/verify_runtime.py \
    && /app/deploy/verify_toolchain.sh \
    && mkdir -p /opt/axiom/release \
    && sha256sum \
         /opt/axiom/runtime/pi/0.82.1/bin/pi \
         /opt/axiom/runtime/node/v22.22.2/bin/node \
         > /opt/axiom/release/SHA256SUMS \
    && python /app/deploy/generate_release_manifest.py \
         --runtime-manifest /opt/axiom/runtime/manifest.json \
         --output /opt/axiom/release/manifest.json \
    && sha256sum /opt/axiom/release/manifest.json >> /opt/axiom/release/SHA256SUMS \
    && mkdir -p /var/lib/axiom /var/lib/axiom-browser-profile \
    && chown -R axiom:axiom /app /var/lib/axiom /var/lib/axiom-browser-profile /opt/axiom
USER axiom
WORKDIR /app/axiom
RUN python /app/deploy/smoke_runtime.py
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["axiom", "--help"]
