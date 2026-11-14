# ─────────────────────────────────────────────────────────────────────────
# Revoluzen Flow — single-binary container
# ─────────────────────────────────────────────────────────────────────────
# One container running everything: API + scheduler + worker pool + DuckDB.
# Frontend is served via Vite dev server in development; production builds
# serve the React bundle from the FastAPI app's static mount.
#
# Multi-stage build keeps the runtime image lean by leaving the build
# toolchain (gcc, libpq-dev, npm) and pip cache out of the shipping image.
#
# For the Plus tier multi-container topology (api + worker + postgres),
# see the Revoluzen Flow Enterprise deployment guide.
# ─────────────────────────────────────────────────────────────────────────

# ── Stage 1: build the frontend bundle ──
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
# Use vite directly — `npm run build` runs `tsc -b && vite build`, and
# tsc strict-mode noise (pre-existing in the 1.0 baseline) would fail
# the image build. Vite's esbuild transpile still emits the production
# bundle correctly; type-only errors don't affect runtime output.
RUN npx vite build

# ── Stage 2: build Python wheels ──
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt \
    && pip install --no-cache-dir --user psutil

# ── Stage 3: runtime ──
FROM python:3.11-slim AS runtime

# Microsoft's ODBC driver for SQL Server, Azure SQL and Azure Synapse comes
# under Microsoft's licence terms, so the image leaves it out unless it is
# built with  --build-arg INSTALL_MSODBCSQL=yes  which accepts those terms.
ARG INSTALL_MSODBCSQL=no

# git is what copies published pipelines to the customer's own repository
# (Settings > Security & Diagnostics). Only used when that is switched on.
# unixodbc is the ODBC driver manager pyodbc loads, for SQL Server and the
# ODBC database connection (2026-11-11); without it pyodbc cannot even import.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates unixodbc \
    && if [ "$INSTALL_MSODBCSQL" = "yes" ]; then \
         apt-get install -y --no-install-recommends curl gnupg \
         && . /etc/os-release \
         && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
              | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
         && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/${VERSION_ID}/prod ${VERSION_CODENAME} main" \
              > /etc/apt/sources.list.d/mssql-release.list \
         && apt-get update \
         && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
         && apt-get purge -y curl gnupg && apt-get autoremove -y; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — Revoluzen Flow never needs root.
RUN groupadd -r revoluzen && useradd -r -g revoluzen -d /app -s /sbin/nologin revoluzen

WORKDIR /app

# Resolved Python deps from builder. They were `pip install --user`ed into
# /root/.local (builder runs as root) and copied here. The runtime user `revoluzen`
# has HOME=/app (useradd -d /app), so Python's default user-site would be
# /app/.local — the WRONG place. PYTHONUSERBASE points Python's user-site at the
# real location so `python -m uvicorn` (and every dep) actually resolves;
# without it the container starts and instantly dies with "No module named
# uvicorn" while the image still builds clean.
COPY --from=builder /root/.local /home/revoluzen/.local
ENV PATH=/home/revoluzen/.local/bin:$PATH \
    PYTHONUSERBASE=/home/revoluzen/.local \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REVOLUZEN_FLOW_DATA_DIR=/data \
    REVOLUZEN_FLOW_PORT=8001 \
    REVOLUZEN_FLOW_MODE=prod \
    REVOLUZEN_FLOW_SECURITY_MODE=server \
    REVOLUZEN_FLOW_BIND_HOST=0.0.0.0

# SECURITY_MODE=server: the container listens on 0.0.0.0 (CMD below), so it is
# reachable by more than the local user. Without this the app resolved "local"
# mode — anonymous access, and the password-reset token returned in the HTTP
# response — and an anonymous network caller could take over the only account
# (2026-11-13 pen test). BIND_HOST tells the loopback-only origin guard that
# this is a network bind; uvicorn started directly never sets the resolved
# bind host the CLI launcher writes.

# Backend source
COPY backend/ /app/backend/
# Frontend bundle (served as static by FastAPI). Must land on the packaged
# path main.py's _resolve_frontend_dist() checks — <pkg>/frontend_dist — or the
# API boots healthy while serving a blank UI (/ 404s, healthcheck still passes).
COPY --from=frontend /build/dist /app/backend/revoluzen_flow/frontend_dist

# Persistent data dir
RUN mkdir -p /data && chown -R revoluzen:revoluzen /app /data /home/revoluzen

USER revoluzen

# The DuckDB extensions pipelines load for cloud files (httpfs, azure) and
# spatial files, installed now so a container without internet access has
# them (2026-11-11). They land in ~/.duckdb, which is this user's /app.
RUN python -c "import duckdb; c = duckdb.connect(); [c.install_extension(e) for e in ('httpfs', 'azure', 'spatial')]"

# Driver add-ons installed on Settings > Add-ons go into the data volume
# (/data/addons/...), so they survive upgrading this image.

EXPOSE 8001

# Liveness probe — uses the lifespan-aware /api/health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:8001/api/health', timeout=3).status == 200 else 1)"

# Single uvicorn worker — Revoluzen Flow is single-node by design. The
# in-process scheduler and worker pool serialize correctly with one
# uvicorn process; multiple workers would duplicate them.
CMD ["python", "-m", "uvicorn", "revoluzen_flow.main:app", "--host", "0.0.0.0", "--port", "8001"]
