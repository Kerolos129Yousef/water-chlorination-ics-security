"""Phase 8A: static validation of the Docker packaging artifacts.

These tests assert the *contract* of the container setup without requiring a
Docker daemon (so they run in any CI): the runtime image excludes test/dataset
material, runs non-root, persists SQLite to a volume outside the image, and the
compose file wires the documented environment. The real build + runtime
behaviour is covered by the Phase 8A docker smoke test (docs/provenance).
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    p = ROOT / rel
    if not p.is_file():
        pytest.fail(f"missing required Docker artifact: {rel}")
    return p.read_text()


def test_docker_artifacts_exist():
    for rel in (
        "docker/backend.Dockerfile",
        "docker/frontend.Dockerfile",
        "docker/requirements-runtime.txt",
        "docker/nginx.conf",
        "docker-compose.yml",
        ".dockerignore",
    ):
        assert (ROOT / rel).is_file(), f"missing {rel}"


def test_backend_dockerfile_is_hardened():
    df = _read("docker/backend.Dockerfile")
    # non-root execution
    assert "USER app" in df
    # deterministic startup = the same app the repo runs locally
    assert 'CMD ["uvicorn", "backend.app:app"' in df
    # healthcheck uses the app's own /health
    assert "HEALTHCHECK" in df and "/health" in df
    # durable SQLite defaults point outside the image layer
    assert "ALERT_STORAGE_BACKEND=sqlite" in df
    assert "ALERT_DB_PATH=/data/alerts.sqlite3" in df
    # multi-stage: build deps do not reach the runtime image
    assert "AS builder" in df and "AS runtime" in df


def test_runtime_requirements_exclude_test_only_deps():
    # Inspect only dependency lines, not the explanatory header comments (which
    # legitimately name the excluded tools).
    dep_lines = [
        ln.split("#", 1)[0].strip().lower()
        for ln in _read("docker/requirements-runtime.txt").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    deps = "\n".join(dep_lines)
    assert "pytest" not in deps, "runtime image must not ship pytest"
    assert "httpx" not in deps, "runtime image must not ship the test-only httpx"
    # pinned runtime core is present
    for pkg in ("fastapi==", "uvicorn", "torch==", "numpy==", "scikit-learn=="):
        assert pkg in deps, f"expected pinned {pkg} in runtime requirements"


def test_runtime_requirements_include_postgres_client():
    # Phase 8B: the image must be able to talk to PostgreSQL when selected.
    deps = _read("docker/requirements-runtime.txt").lower()
    assert "psycopg" in deps, "runtime image must ship psycopg for the postgres backend"
    assert "psycopg-pool" in deps, "postgres backend uses a connection pool"


def test_dockerignore_excludes_dataset_and_nonruntime():
    di = _read(".dockerignore")
    for pattern in ("*.csv", "tests/", "*.sqlite3", ".venv", "ml/configs/"):
        assert pattern in di, f".dockerignore should exclude {pattern}"


def test_compose_wires_sqlite_volume_and_hardening():
    c = _read("docker-compose.yml")
    assert "ALERT_STORAGE_BACKEND: sqlite" in c
    assert "ALERT_DB_PATH: /data/alerts.sqlite3" in c
    # runtime DB persisted to a named volume, mounted at /data
    assert "alert-data:/data" in c
    assert "read_only: true" in c
    assert "no-new-privileges:true" in c
    # both services published on their documented ports
    assert "8000:8000" in c
    assert "8080:80" in c
    # the base file stays SQLite-only: the default `up` flow is unchanged.
    assert "ALERT_STORAGE_BACKEND: postgres" not in c


# ------------------------------------------------------ Phase 8B: postgres overlay


def test_postgres_overlay_and_env_example_exist():
    for rel in ("docker-compose.postgres.yml", ".env.example"):
        assert (ROOT / rel).is_file(), f"missing {rel}"


def test_postgres_overlay_wires_service_volume_and_healthcheck():
    c = _read("docker-compose.postgres.yml")
    # a postgres service with a persistent named volume + healthcheck
    assert "postgres:" in c
    assert "postgres-data:/var/lib/postgresql/data" in c
    assert "pg_isready" in c
    # backend switched to the postgres backend and waits for a HEALTHY db
    assert "ALERT_STORAGE_BACKEND: postgres" in c
    assert "condition: service_healthy" in c
    assert "no-new-privileges:true" in c


def test_postgres_overlay_commits_no_secrets():
    c = _read("docker-compose.postgres.yml")
    # credentials come from the environment (${...}), never literals.
    assert "${POSTGRES_PASSWORD" in c
    # postgres is not published to the host (internal compose network only).
    assert "5432:5432" not in c


def test_env_example_has_no_real_password():
    e = _read(".env.example")
    for var in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        assert var in e, f".env.example should document {var}"
    # a placeholder, not a real secret
    assert "change-me" in e.lower()


def test_dockerignore_excludes_env_file():
    di = _read(".dockerignore")
    assert ".env" in di, ".dockerignore should exclude the .env credentials file"
