"""Phase 8B integration tests: :class:`PostgreSQLAlertStore` against a REAL server.

The whole point of this phase is that the persistence abstraction can move from
local SQLite to a network database with **no** change to ``AlertEngine`` semantics.
Mocking Postgres would leave the real SQL path untested, so these tests run against
an actual PostgreSQL instance:

* if ``ALERT_TEST_PG_DSN`` is set, that database is used;
* otherwise a throwaway ``postgres:16-alpine`` container is started via Docker for
  the module and torn down afterwards;
* if neither Docker nor a DSN is available, the whole module is skipped (so the
  offline unit suite still passes).

Coverage mirrors ``tests/test_alert_store.py`` + ``tests/test_alert_persistence.py``
for the Postgres backend: construction/schema, insert/upsert, update/extend, close,
category/status queries, history ordering, category-specific round-trips, corrupt
record handling, restart recovery for both categories, id continuity, and env
selection. The invalid-configuration cases need no server and always run.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest

from alerting import (
    AlertEngine,
    AlertStatus,
    AlertStoreError,
    PROCESS_ANOMALY_CATEGORY,
    PostgreSQLAlertStore,
    Severity,
    TELEMETRY_FAULT_CATEGORY,
    alert_store_from_env,
)
from alerting.alert import Alert

PA = PROCESS_ANOMALY_CATEGORY
TF = TELEMETRY_FAULT_CATEGORY

_PG_IMAGE = "postgres:16-alpine"
_PG_USER = "ics_app"
_PG_DB = "ics_alerts_test"
_PG_PASSWORD = "test-only-password"  # ephemeral test container; never a real secret


# ------------------------------------------------------------------- helpers


def _pa(alert_id="alert-0001-w1", status=AlertStatus.OPEN):
    return Alert(
        alert_id=alert_id, category=PA, status=status,
        detected_at="w1", window_start="w1", window_end="w1",
        anomaly_score=1.0e-3, threshold=9.269e-5, is_anomaly=True,
        top_features=[("LIT101", 0.5), ("FIT101", 0.3)], window_count=1,
        severity=Severity.HIGH, opened_at="w1", closed_at=None,
    )


def _tf(alert_id="tfault-0001-FIT101-t1", status=AlertStatus.OPEN, channel="FIT101"):
    return Alert(
        alert_id=alert_id, category=TF, status=status,
        detected_at="t1", window_start="t1", window_end="t1",
        anomaly_score=0.0, threshold=0.0, is_anomaly=False,
        top_features=[], window_count=1, severity=Severity.MEDIUM,
        opened_at="t1", closed_at=None,
        affected_channels=[channel], reason=f"STUCK_CHANNEL: {channel}",
        staleness_seconds=20.0,
    )


def _det(is_anomaly, score, threshold=9.269e-5):
    return SimpleNamespace(
        is_anomaly=is_anomaly, anomaly_score=score, threshold=threshold,
        top_features=lambda n: [("LIT101", 0.5), ("FIT101", 0.3)][:n],
    )


def _health(healthy, channels, ts=None):
    stuck = tuple(
        SimpleNamespace(feature=f, unchanged_samples=n, staleness_seconds=(n - 1) * 5.0)
        for f, n in channels
    )
    return SimpleNamespace(healthy=healthy, stuck_channels=stuck, timestamp=ts)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _docker_available() -> bool:
    from shutil import which

    if which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"], check=True, capture_output=True, timeout=15
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def pg_dsn():
    """A live PostgreSQL DSN: an external one via env, else a throwaway container."""
    external = os.environ.get("ALERT_TEST_PG_DSN")
    if external:
        yield external
        return

    if not _docker_available():
        pytest.skip(
            "no ALERT_TEST_PG_DSN and Docker unavailable; skipping real-Postgres tests"
        )

    port = _free_port()
    name = f"ics-pg-test-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e", f"POSTGRES_USER={_PG_USER}",
                "-e", f"POSTGRES_DB={_PG_DB}",
                "-p", f"127.0.0.1:{port}:5432",
                _PG_IMAGE,
            ],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not start test Postgres container: {exc}")

    dsn = (
        f"host=127.0.0.1 port={port} dbname={_PG_DB} "
        f"user={_PG_USER} password={_PG_PASSWORD}"
    )
    try:
        # Wait for the server to accept connections (schema init proves readiness).
        deadline = time.time() + 60
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                probe = PostgreSQLAlertStore(dsn, connect_timeout=3)
                probe.close()
                break
            except AlertStoreError as exc:  # not ready yet
                last_err = exc
                time.sleep(1.0)
        else:
            pytest.skip(f"test Postgres never became ready: {last_err}")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(autouse=True)
def _clean_db(request):
    """Isolate every test: truncate the table before it runs (shared container)."""
    if "pg_dsn" not in request.fixturenames:
        return  # a test that needs no server (invalid-config cases)
    dsn = request.getfixturevalue("pg_dsn")
    s = PostgreSQLAlertStore(dsn)
    s.clear()
    s.close()


@pytest.fixture
def store(pg_dsn):
    s = PostgreSQLAlertStore(pg_dsn)
    yield s
    s.close()


def _engine(pg_dsn):
    """A fresh engine bound to the Postgres DB -- simulates a process (re)start."""
    return AlertEngine(store=PostgreSQLAlertStore(pg_dsn))


# --------------------------------------------------- construction / schema / I/O


def test_construction_creates_schema_and_starts_empty(store):
    assert store.query() == []  # table created, no rows


def test_upsert_insert_then_query(store):
    a = _pa()
    store.upsert(a)
    got = store.query()
    assert len(got) == 1
    assert got[0].alert_id == a.alert_id
    assert got[0].category == PA
    assert got[0].status is AlertStatus.OPEN


def test_upsert_updates_in_place_no_duplicate(store):
    a = _pa()
    store.upsert(a)
    a.window_count = 5
    a.status = AlertStatus.CLOSED
    a.closed_at = "w9"
    store.upsert(a)
    rows = store.query()
    assert len(rows) == 1
    assert rows[0].window_count == 5
    assert rows[0].status is AlertStatus.CLOSED
    assert rows[0].closed_at == "w9"


def test_open_order_is_preserved_across_updates(store):
    a1, a2, a3 = _pa("alert-0001-w1"), _pa("alert-0002-w2"), _pa("alert-0003-w3")
    for a in (a1, a2, a3):
        store.upsert(a)
    a1.window_count = 9  # update the first -> order must not change
    store.upsert(a1)
    ids = [a.alert_id for a in store.query()]
    assert ids == ["alert-0001-w1", "alert-0002-w2", "alert-0003-w3"]


def test_close_is_idempotent(store):
    store.upsert(_pa())
    store.close()
    store.close()  # closing a pool twice must not raise


# ------------------------------------------------------------------- filtering


def test_query_filters_by_category_status_limit(store):
    store.upsert(_pa("alert-0001-w1", status=AlertStatus.OPEN))
    store.upsert(_pa("alert-0002-w2", status=AlertStatus.CLOSED))
    store.upsert(_tf("tfault-0001-FIT101-t1", status=AlertStatus.OPEN))

    assert {a.alert_id for a in store.query(category=PA)} == {"alert-0001-w1", "alert-0002-w2"}
    assert [a.alert_id for a in store.query(category=TF)] == ["tfault-0001-FIT101-t1"]
    assert [a.alert_id for a in store.query(status=AlertStatus.OPEN, category=PA)] == ["alert-0001-w1"]
    assert [a.alert_id for a in store.query(status="closed")] == ["alert-0002-w2"]
    assert len(store.query(limit=1)) == 1


# ------------------------------------------------ category-specific round-trip


def test_process_anomaly_fields_round_trip(store):
    store.upsert(_pa())
    a = store.query(category=PA)[0]
    assert a.is_anomaly is True
    assert a.top_features == [("LIT101", 0.5), ("FIT101", 0.3)]
    assert a.severity is Severity.HIGH
    assert a.affected_channels == []
    assert a.reason is None and a.staleness_seconds is None


def test_telemetry_fault_fields_round_trip(store):
    store.upsert(_tf(channel="AIT201"))
    a = store.query(category=TF)[0]
    assert a.is_anomaly is False
    assert a.affected_channels == ["AIT201"]
    assert a.reason == "STUCK_CHANNEL: AIT201"
    assert a.staleness_seconds == pytest.approx(20.0)
    assert a.top_features == []


def test_clear_removes_everything(store):
    store.upsert(_pa())
    store.upsert(_tf())
    store.clear()
    assert store.query() == []


# ------------------------------------------------------- corrupt data / safety


def test_corrupt_row_is_skipped_not_fatal(store, pg_dsn):
    import psycopg

    store.upsert(_pa("alert-0001-w1"))
    store.upsert(_pa("alert-0002-w2"))
    # Corrupt one row's JSONB shape directly (valid JSON, wrong structure).
    with psycopg.connect(pg_dsn) as conn:
        conn.execute(
            "UPDATE alerts SET top_features = '{\"bad\": true}'::jsonb "
            "WHERE alert_id = 'alert-0001-w1'"
        )
        conn.commit()
    rows = store.query()  # must not raise; skips the bad row
    assert [a.alert_id for a in rows] == ["alert-0002-w2"]


# ----------------------------------------------------- restart recovery: PA


def test_process_anomaly_survives_restart_full_lifecycle(pg_dsn):
    e1 = _engine(pg_dsn)
    opened = e1.process(_det(True, 1.0e-3), window_start="w0", window_end="w1")
    e1.process(_det(True, 2.0e-3), window_end="w2")     # extend (dedup)
    assert e1.active_alert.window_count == 2
    e1.store.close()

    e2 = _engine(pg_dsn)                                # "restart"
    restored = e2.active_alert
    assert restored is not None
    assert restored.alert_id == opened.alert_id         # same incident, not a new one
    assert restored.status is AlertStatus.OPEN
    assert restored.window_count == 2

    e2.process(_det(True, 3.0e-3), window_end="w3")     # dedup continues
    assert e2.active_alert.window_count == 3
    assert len([a for a in e2.alerts if a.category == PA]) == 1

    closed = e2.process(_det(False, 1.0e-6), window_end="w4")
    assert closed.status is AlertStatus.CLOSED
    assert e2.active_alert is None
    e2.store.close()

    e3 = _engine(pg_dsn)
    history = [a for a in e3.alerts if a.category == PA]
    assert len(history) == 1
    assert history[0].alert_id == opened.alert_id
    assert history[0].status is AlertStatus.CLOSED
    assert history[0].closed_at == "w4"
    assert e3.active_alert is None
    e3.store.close()


# ----------------------------------------------------- restart recovery: TF


def test_telemetry_fault_survives_restart_full_lifecycle(pg_dsn):
    e1 = _engine(pg_dsn)
    opened = e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")[0]
    e1.process_health(_health(False, [("FIT101", 6)]), timestamp="t2")   # dedup
    assert e1.active_telemetry_faults[0].window_count == 2
    e1.store.close()

    e2 = _engine(pg_dsn)
    restored = e2.active_telemetry_faults
    assert len(restored) == 1
    assert restored[0].alert_id == opened.alert_id
    assert restored[0].affected_channels == ["FIT101"]
    assert restored[0].window_count == 2

    e2.process_health(_health(False, [("FIT101", 7)]), timestamp="t3")
    assert e2.active_telemetry_faults[0].window_count == 3
    assert len([a for a in e2.alerts if a.category == TF]) == 1

    closed = e2.process_health(_health(True, []), timestamp="t4")[0]
    assert closed.status is AlertStatus.CLOSED
    assert e2.active_telemetry_faults == ()
    e2.store.close()

    e3 = _engine(pg_dsn)
    hist = [a for a in e3.alerts if a.category == TF]
    assert len(hist) == 1
    assert hist[0].status is AlertStatus.CLOSED and hist[0].closed_at == "t4"
    assert e3.active_telemetry_faults == ()
    e3.store.close()


def test_both_categories_coexist_across_restart(pg_dsn):
    e1 = _engine(pg_dsn)
    e1.process(_det(True, 1.0e-3), window_end="w1")
    e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")
    e1.store.close()

    e2 = _engine(pg_dsn)
    assert e2.active_alert is not None
    assert len(e2.active_telemetry_faults) == 1
    assert {a.category for a in e2.alerts} == {PA, TF}
    e2.store.close()


def test_deterministic_ids_do_not_collide_after_restart(pg_dsn):
    e1 = _engine(pg_dsn)
    first = e1.process(_det(True, 1.0e-3), window_end="w1")
    e1.process(_det(False, 1e-6), window_end="w2")            # close it
    tf1 = e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")[0]
    e1.store.close()

    e2 = _engine(pg_dsn)
    second = e2.process(_det(True, 1.0e-3), window_end="w3")
    tf2 = e2.process_health(_health(False, [("AIT201", 5)]), timestamp="t2")[0]
    assert second.alert_id != first.alert_id
    assert second.alert_id.startswith("alert-0002-")
    assert tf2.alert_id != tf1.alert_id
    assert tf2.alert_id.startswith("tfault-0002-")
    ids = [a.alert_id for a in e2.alerts]
    assert len(ids) == len(set(ids))                         # all unique
    e2.store.close()


# ----------------------------------------------------------- configuration


def test_env_selects_postgres_via_dsn(pg_dsn):
    s = alert_store_from_env({"ALERT_STORAGE_BACKEND": "postgres", "ALERT_PG_DSN": pg_dsn})
    try:
        assert isinstance(s, PostgreSQLAlertStore)
        assert s.query() == []
    finally:
        s.close()


def test_env_selects_postgres_via_parts(pg_dsn):
    # Parse the container DSN back into POSTGRES_* parts to exercise make_conninfo.
    import psycopg

    parts = psycopg.conninfo.conninfo_to_dict(pg_dsn)
    env = {
        "ALERT_STORAGE_BACKEND": "postgres",
        "POSTGRES_HOST": parts["host"],
        "POSTGRES_PORT": str(parts["port"]),
        "POSTGRES_DB": parts["dbname"],
        "POSTGRES_USER": parts["user"],
        "POSTGRES_PASSWORD": parts["password"],
    }
    s = alert_store_from_env(env)
    try:
        assert isinstance(s, PostgreSQLAlertStore)
        s.upsert(_pa())
        assert len(s.query()) == 1
    finally:
        s.close()


# --------------------------- invalid configuration (no server needed) ---------


def test_env_postgres_missing_credentials_is_rejected():
    with pytest.raises(AlertStoreError, match="POSTGRES_DB|ALERT_PG_DSN"):
        alert_store_from_env({"ALERT_STORAGE_BACKEND": "postgres"})


def test_empty_conninfo_is_rejected():
    with pytest.raises(AlertStoreError, match="non-empty"):
        PostgreSQLAlertStore("   ")


def test_unreachable_postgres_raises_clear_error():
    with pytest.raises(AlertStoreError, match="could not open PostgreSQL"):
        PostgreSQLAlertStore(
            "host=127.0.0.1 port=1 dbname=x user=y password=z", connect_timeout=2
        )
