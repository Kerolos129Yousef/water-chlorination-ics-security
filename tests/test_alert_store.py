"""AlertStore unit tests (Phase 7) -- both backends, no ML, no network, fast.

Covers the storage contract shared by :class:`InMemoryAlertStore` and
:class:`SQLiteAlertStore`: SQLite file creation, insert/update upsert semantics
(no duplicate rows, open order preserved), category/status/limit queries,
category-specific field round-tripping, clear, safe handling of a corrupt row, and
the ``alert_store_from_env`` configuration.
"""

from __future__ import annotations

import sqlite3

import pytest

from alerting.alert import Alert, AlertStatus, Severity
from alerting.store import (
    AlertStoreError,
    InMemoryAlertStore,
    SQLiteAlertStore,
    alert_store_from_env,
)

PA = "PROCESS_ANOMALY"
TF = "TELEMETRY_FAULT"


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


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """A store of each backend; the SQLite one uses a temp DB file."""
    if request.param == "memory":
        s = InMemoryAlertStore()
    else:
        s = SQLiteAlertStore(tmp_path / "nested" / "alerts.sqlite3")
    yield s
    s.close()


# ---------------------------------------------------------- creation / basic I/O


def test_sqlite_creates_file_and_parent_dirs(tmp_path):
    db = tmp_path / "a" / "b" / "alerts.sqlite3"
    assert not db.exists()
    s = SQLiteAlertStore(db)
    try:
        assert db.exists()               # file + parent dirs auto-created
        assert s.query() == []           # fresh DB is empty
    finally:
        s.close()


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
    # Same id, mutated -> update, not a second row.
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
    # Update the first one; order must not change.
    a1.window_count = 9
    store.upsert(a1)
    ids = [a.alert_id for a in store.query()]
    assert ids == ["alert-0001-w1", "alert-0002-w2", "alert-0003-w3"]


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
    assert a.affected_channels == []      # PA carries no channels
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


def test_two_memory_stores_are_isolated():
    a, b = InMemoryAlertStore(), InMemoryAlertStore()
    a.upsert(_pa())
    assert a.query() and b.query() == []


# ------------------------------------------------------- corrupt data / safety


def test_corrupt_row_is_skipped_not_fatal(tmp_path):
    db = tmp_path / "alerts.sqlite3"
    s = SQLiteAlertStore(db)
    s.upsert(_pa("alert-0001-w1"))
    s.upsert(_pa("alert-0002-w2"))
    s.close()

    # Corrupt one row's JSON directly on disk.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE alerts SET top_features='{not valid json' WHERE alert_id='alert-0001-w1'")
    conn.commit()
    conn.close()

    s2 = SQLiteAlertStore(db)
    try:
        rows = s2.query()               # must not raise; skips the bad row
        assert [a.alert_id for a in rows] == ["alert-0002-w2"]
    finally:
        s2.close()


# ----------------------------------------------------------- configuration


def test_env_defaults_to_memory():
    assert isinstance(alert_store_from_env({}), InMemoryAlertStore)


def test_env_selects_sqlite(tmp_path):
    db = tmp_path / "cfg.sqlite3"
    s = alert_store_from_env({"ALERT_STORAGE_BACKEND": "sqlite", "ALERT_DB_PATH": str(db)})
    try:
        assert isinstance(s, SQLiteAlertStore)
        assert db.exists()
    finally:
        s.close()


def test_env_unknown_backend_is_rejected():
    with pytest.raises(AlertStoreError, match="unknown"):
        alert_store_from_env({"ALERT_STORAGE_BACKEND": "postgres"})


def test_env_backend_is_case_insensitive(tmp_path):
    s = alert_store_from_env({"ALERT_STORAGE_BACKEND": "  SQLite  ",
                              "ALERT_DB_PATH": str(tmp_path / "x.sqlite3")})
    try:
        assert isinstance(s, SQLiteAlertStore)
    finally:
        s.close()
