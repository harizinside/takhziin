# syntax=docker/dockerfile:1.7
# takhziin — multi-stage build, single runtime image with native dump tools.
#
# Base: python:3.11-slim-bookworm (Debian 12) for amd64 + arm64.
# Native dump tools installed from upstream repos in one layer:
#   - mariadb-client    → mariadb-dump, mariadb
#   - postgresql-client → pg_dump, pg_restore
#   - mongodb-database-tools (MongoDB Inc official repo) → mongodump, mongorestore
#   - mongosh (CLI for live db)
# Plus: tini (PID 1), ca-certificates, tzdata.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# OS deps needed only for building wheels (psycopg, mysql-connector, cryptography).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        pkg-config \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

# Install takhziin + deps into a clean prefix dir so we can copy to runtime.
RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM ${PYTHON_IMAGE} AS runtime

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TAKHZIIN_DATA_DIR=/data \
    TAKHZIIN_CONFIG_DIR=/data/config \
    TAKHZIIN_BIN_DIR=/usr/bin \
    TAKHZIIN_BIND_HOST=0.0.0.0 \
    TAKHZIIN_BIND_PORT=8765 \
    TZ=UTC

# Native dump tools + tini + ca-certificates + tzdata, installed in a single layer
# to keep image size bounded. MongoDB official repo is added inline for
# mongodb-database-tools (not in Debian main).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        tini \
        gnupg \
        curl \
        mariadb-client \
        postgresql-client; \
    \
    # MongoDB official APT repo (Debian 12 / bookworm). Only the database-tools
    # package is pulled — the mongod server is NOT.
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc -o /usr/share/keyrings/mongodb-server-7.0.asc; \
    echo "deb [signed-by=/usr/share/keyrings/mongodb-server-7.0.asc] http://repo.mongodb.org/apt/debian bookworm/mongodb-org/7.0 main" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        mongodb-database-tools \
        mongosh; \
    \
    rm -rf /var/lib/apt/lists/*; \
    \
    # Sanity: every native dump tool must resolve
    which mongodump pg_dump mariadb-dump mongorestore pg_restore mariadb mongosh

# Copy Python install from builder
COPY --from=builder /install /usr/local

# Copy source tree (small) so 'python -m takhziin.cli' resolves
WORKDIR /app
COPY src ./src

# Persistent data dir owned by the unprivileged 'takhziin' user we create next.
RUN groupadd --system takhziin && useradd --system --gid takhziin --uid 1000 takhziin \
    && mkdir -p /data /data/backups \
    && chown -R takhziin:takhziin /data

USER takhziin

VOLUME ["/data"]

EXPOSE 8765

# Healthcheck: probe the bind socket via the same code path docker-compose runs
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "takhziin.cli", "healthcheck"]

ENTRYPOINT ["tini", "--", "python", "-m", "takhziin.cli"]
CMD ["ui", "--host", "0.0.0.0", "--port", "8765"]