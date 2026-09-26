"""Phase 7 restart-recovery integration: AlertEngine over a durable store.

The core requirement: a process restart (a *new* AlertEngine on the *same* SQLite
database) restores active incidents and history for BOTH categories, continues
deduplication and closure with no duplicate incidents, and never mints a colliding
deterministic id. Covers the two mandated realistic restart tests (PROCESS_ANOMALY
and TELEMETRY_FAULT) plus the supporting cases.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from alerting import (
    AlertEngine,
    AlertStatus,
    InMemoryAlertStore,
    PROCESS_ANOMALY_CATEGORY,
    SQLiteAlertStore,
    TELEMETRY_FAULT_CATEGORY,
)
from backend.app import create_app


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


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "alerts.sqlite3"


def _engine(db_path):
    """A fresh engine bound to the SQLite db -- simulates a process (re)start."""
    return AlertEngine(store=SQLiteAlertStore(db_path))


# ============================================================ mandated: PROCESS_ANOMALY


def test_process_anomaly_survives_restart_full_lifecycle(db_path):
    e1 = _engine(db_path)
    opened = e1.process(_det(True, 1.0e-3), window_start="w0", window_end="w1")
    e1.process(_det(True, 2.0e-3), window_end="w2")     # extend (dedup)
    assert e1.active_alert.window_count == 2
    e1.store.close()                                    # "shut down"

    # Restart: a brand-new engine on the same database.
    e2 = _engine(db_path)
    restored = e2.active_alert
    assert restored is not None
    assert restored.alert_id == opened.alert_id         # same incident, not a new one
    assert restored.status is AlertStatus.OPEN
    assert restored.window_count == 2                   # extension persisted

    # Continue processing: dedup MUST continue (no duplicate incident).
    e2.process(_det(True, 3.0e-3), window_end="w3")
    assert e2.active_alert.window_count == 3
    assert len([a for a in e2.alerts if a.category == PROCESS_ANOMALY_CATEGORY]) == 1

    # Close, then verify the history holds exactly one complete lifecycle.
    closed = e2.process(_det(False, 1.0e-6), window_end="w4")
    assert closed.status is AlertStatus.CLOSED
    assert e2.active_alert is None
    e2.store.close()

    e3 = _engine(db_path)
    history = [a for a in e3.alerts if a.category == PROCESS_ANOMALY_CATEGORY]
    assert len(history) == 1
    assert history[0].alert_id == opened.alert_id
    assert history[0].status is AlertStatus.CLOSED
    assert history[0].closed_at == "w4"
    assert e3.active_alert is None
    e3.store.close()


# ============================================================ mandated: TELEMETRY_FAULT


def test_telemetry_fault_survives_restart_full_lifecycle(db_path):
    e1 = _engine(db_path)
    opened = e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")[0]
    e1.process_health(_health(False, [("FIT101", 6)]), timestamp="t2")   # dedup
    assert e1.active_telemetry_faults[0].window_count == 2
    e1.store.close()

    e2 = _engine(db_path)
    restored = e2.active_telemetry_faults
    assert len(restored) == 1
    assert restored[0].alert_id == opened.alert_id
    assert restored[0].affected_channels == ["FIT101"]
    assert restored[0].window_count == 2

    # Dedup continues on the SAME per-channel incident.
    e2.process_health(_health(False, [("FIT101", 7)]), timestamp="t3")
    assert e2.active_telemetry_faults[0].window_count == 3
    assert len([a for a in e2.alerts if a.category == TELEMETRY_FAULT_CATEGORY]) == 1

    # Recovery closes it; history holds exactly one complete lifecycle.
    closed = e2.process_health(_health(True, []), timestamp="t4")[0]
    assert closed.status is AlertStatus.CLOSED
    assert e2.active_telemetry_faults == ()
    e2.store.close()

    e3 = _engine(db_path)
    hist = [a for a in e3.alerts if a.category == TELEMETRY_FAULT_CATEGORY]
    assert len(hist) == 1
    assert hist[0].status is AlertStatus.CLOSED and hist[0].closed_at == "t4"
    assert e3.active_telemetry_faults == ()
    e3.store.close()


# ======================================================= supporting restart cases


def test_multiple_active_telemetry_channels_survive_restart(db_path):
    e1 = _engine(db_path)
    e1.process_health(_health(False, [("FIT101", 5), ("AIT201", 5), ("PIT501", 5)]), timestamp="t1")
    e1.store.close()

    e2 = _engine(db_path)
    channels = sorted(a.affected_channels[0] for a in e2.active_telemetry_faults)
    assert channels == ["AIT201", "FIT101", "PIT501"]
    # Recover just one; the other two remain active across the restart boundary.
    e2.process_health(_health(False, [("FIT101", 6), ("PIT501", 6)]), timestamp="t2")
    assert sorted(a.affected_channels[0] for a in e2.active_telemetry_faults) == ["FIT101", "PIT501"]
    e2.store.close()


def test_both_categories_coexist_across_restart(db_path):
    e1 = _engine(db_path)
    e1.process(_det(True, 1.0e-3), window_end="w1")
    e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")
    e1.store.close()

    e2 = _engine(db_path)
    assert e2.active_alert is not None
    assert len(e2.active_telemetry_faults) == 1
    assert {a.category for a in e2.alerts} == {PROCESS_ANOMALY_CATEGORY, TELEMETRY_FAULT_CATEGORY}
    e2.store.close()


def test_deterministic_ids_do_not_collide_after_restart(db_path):
    e1 = _engine(db_path)
    first = e1.process(_det(True, 1.0e-3), window_end="w1")   # alert-0001-...
    e1.process(_det(False, 1e-6), window_end="w2")            # close it
    tf1 = e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")[0]  # tfault-0001-...
    e1.store.close()

    e2 = _engine(db_path)
    # Counters resumed: new incidents get the NEXT sequence, never a duplicate.
    second = e2.process(_det(True, 1.0e-3), window_end="w3")
    tf2 = e2.process_health(_health(False, [("AIT201", 5)]), timestamp="t2")[0]
    assert second.alert_id != first.alert_id
    assert second.alert_id.startswith("alert-0002-")
    assert tf2.alert_id != tf1.alert_id
    assert tf2.alert_id.startswith("tfault-0002-")
    ids = [a.alert_id for a in e2.alerts]
    assert len(ids) == len(set(ids))                         # all unique
    e2.store.close()


def test_reopen_after_recovery_is_a_new_incident_across_restart(db_path):
    e1 = _engine(db_path)
    e1.process_health(_health(False, [("FIT101", 5)]), timestamp="t1")
    e1.process_health(_health(True, []), timestamp="t2")     # closed
    e1.store.close()

    e2 = _engine(db_path)
    assert e2.active_telemetry_faults == ()                  # nothing active to restore
    reopened = e2.process_health(_health(False, [("FIT101", 5)]), timestamp="t3")[0]
    assert reopened.alert_id.startswith("tfault-0002-")      # distinct incident
    faults = [a for a in e2.alerts if a.category == TELEMETRY_FAULT_CATEGORY]
    assert len(faults) == 2
    e2.store.close()


# ---------------------------------------------------- in-memory backend retained


def test_in_memory_backend_has_no_cross_instance_persistence():
    store = InMemoryAlertStore()
    e1 = AlertEngine(store=store)
    e1.process(_det(True, 1e-3), window_end="w1")
    # A separate in-memory store == a fresh process with no durability.
    e2 = AlertEngine(store=InMemoryAlertStore())
    assert e2.alerts == ()
    assert e2.active_alert is None


def test_default_engine_is_in_memory_and_isolated():
    e1, e2 = AlertEngine(), AlertEngine()
    e1.process(_det(True, 1e-3), window_end="w1")
    assert len(e1.alerts) == 1
    assert e2.alerts == () and e2.active_alert is None


# ------------------------------------------------------- API over a durable store


def test_api_reflects_recovered_state_after_restart(detector, db_path):
    """The backend, restarted on the same DB, serves the recovered incidents."""
    normal_end = "2015-12-28T10:02:25"

    # Session 1: drive a telemetry fault in through the real /score flow.
    app1 = TestClient(create_app(detector=detector, alert_engine=_engine(db_path)))
    body = {
        "feature_names": list(detector.feature_names),
        "window": [[0.0] * 45 for _ in range(30)],
        "window_end": normal_end,
        "telemetry_health": {
            "healthy": False, "fault_type": "TELEMETRY_FAULT", "timestamp": normal_end,
            "stuck_channels": [{"feature": "FIT101", "unchanged_samples": 2880,
                                "staleness_seconds": 14395.0}],
        },
    }
    assert app1.post("/score", json=body).status_code == 200
    assert app1.get("/status").json()["active_telemetry_fault_count"] == 1
    app1.app.state.alert_engine.store.close()

    # Session 2: brand-new app + engine on the same DB -> fault still visible.
    app2 = TestClient(create_app(detector=detector, alert_engine=_engine(db_path)))
    status = app2.get("/status").json()
    assert status["active_telemetry_fault_count"] == 1
    assert status["telemetry_fault_channels"] == ["FIT101"]
    faults = app2.get("/alerts?category=TELEMETRY_FAULT").json()
    assert len(faults) == 1 and faults[0]["affected_channels"] == ["FIT101"]
    app2.app.state.alert_engine.store.close()
