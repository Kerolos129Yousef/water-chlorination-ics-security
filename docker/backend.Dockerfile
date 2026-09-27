# Phase 8A - Backend container for the Water Chlorination ICS TranAD API.
#
# Reproducible, CPU-only, non-root packaging of the CURRENT application. No
# functional change: the image runs the same `uvicorn backend.app:app` the repo
# runs locally. ML artifacts are copied read-only and never mutated; the SWaT
# dataset is NOT baked in (the API does not need it to start).
#
# Multi-stage: a builder assembles a virtualenv with pinned deps; the runtime
# stage carries only that venv + the runtime source, so build toolchain and pip
# caches never reach the final image.

# ----------------------------------------------------------------- builder stage
FROM python:3.10-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Self-contained virtualenv so the runtime stage copies one directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install deps first (own layer) for cache reuse across source changes. torch
# comes from the CPU wheel index; everything else from PyPI.
COPY docker/requirements-runtime.txt /tmp/requirements-runtime.txt
RUN pip install --upgrade pip && \
    pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        -r /tmp/requirements-runtime.txt

# ----------------------------------------------------------------- runtime stage
FROM python:3.10-slim AS runtime

# PYTHONDONTWRITEBYTECODE keeps the source tree writable-free at runtime (works
# with a read-only root filesystem). ALERT_* default to durable SQLite under the
# mounted data volume; compose sets them explicitly too.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ALERT_STORAGE_BACKEND=sqlite \
    ALERT_DB_PATH=/data/alerts.sqlite3

# Non-root runtime identity (fixed uid/gid for predictable volume ownership).
RUN groupadd --system --gid 10001 app && \
    useradd  --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app

# Pinned dependencies from the builder.
COPY --from=builder /opt/venv /opt/venv

# Runtime source ONLY. No tests, no dataset, no ml/configs, no docs.
# `ml` stays an implicit namespace package (no ml/__init__.py), matching the repo.
COPY backend/       ./backend/
COPY alerting/      ./alerting/
COPY ml/src/        ./ml/src/
COPY ml/artifacts/  ./ml/artifacts/

# /data is the durable SQLite location, owned by the non-root user. The app
# source stays owned by root and is never written to (read-only friendly).
RUN mkdir -p /data && chown -R app:app /data

USER app
EXPOSE 8000

# Liveness/readiness via the app's own /health (200 => detector loaded).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# Deterministic startup: same command as local, bound to all interfaces.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
