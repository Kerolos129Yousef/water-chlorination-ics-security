"""Alert persistence (Phase 7): durable storage *behind* the AlertEngine.

The engine still owns every lifecycle decision (open / extend / close / dedup /
severity / category). This module only makes the resulting :class:`~alerting.alert.Alert`
records **durable** so they survive a process restart::

    signals ─▶ AlertEngine (lifecycle) ─▶ AlertStore (durable) ─▶ Monitoring API ─▶ Dashboard

Two interchangeable backends behind one small interface:

* :class:`InMemoryAlertStore` -- the default. No file, no durability across
  processes; behaviour is identical to the pre-Phase-7 engine (used by unit tests
  and any caller that does not want persistence).
* :class:`SQLiteAlertStore` -- a single local SQLite file. Durable across
  restarts, no server to install, created automatically. The natural precursor to
  a PostgreSQL/cloud store: swapping the backend never changes engine semantics.

The store is deliberately dumb: it stores and returns whole ``Alert`` records and
does **no** lifecycle reasoning. Each write is a single-row upsert (atomic in
SQLite), so a lifecycle step can never leave a half-written multi-row state.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from .alert import Alert, AlertStatus, Severity

__all__ = [
    "AlertStore",
    "InMemoryAlertStore",
    "SQLiteAlertStore",
    "AlertStoreError",
    "alert_store_from_env",
]

logger = logging.getLogger("alerting.store")

_ENV_BACKEND = "ALERT_STORAGE_BACKEND"
_ENV_DB_PATH = "ALERT_DB_PATH"
_DEFAULT_DB_PATH = "alerts.sqlite3"


class AlertStoreError(RuntimeError):
    """Raised on a persistence configuration or I/O failure that must not be hidden."""


@runtime_checkable
class AlertStore(Protocol):
    """Durable storage for :class:`Alert` records. Minimal, backend-agnostic.

    ``query`` returns records in **open order** (the order they were first
    inserted), which is the ordering the monitoring API and the pre-Phase-7 engine
    both assume. Implementations must make ``upsert`` idempotent on ``alert_id``
    (update in place, never duplicate) and must preserve open order across updates.
    """

    def upsert(self, alert: Alert) -> None:
        """Insert ``alert`` or update the existing row with the same ``alert_id``."""
        ...

    def query(
        self,
        *,
        category: str | None = None,
        status: AlertStatus | str | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        """Return matching records in open order (optionally filtered / capped)."""
        ...

    def clear(self) -> None:
        """Remove every stored record (used by ``AlertEngine.reset``)."""
        ...

    def close(self) -> None:
        """Release any underlying resources (a no-op for in-memory)."""
        ...


def _status_value(status: AlertStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if isinstance(status, AlertStatus) else str(status).upper()


# --------------------------------------------------------------------- in-memory


class InMemoryAlertStore:
    """Non-durable store: an ordered dict of live ``Alert`` objects.

    Stores the *same* object references the engine holds, so in-place lifecycle
    mutations are reflected without copying -- exactly the pre-Phase-7 behaviour.
    A fresh instance is empty, so two instances never share state (isolation).
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Alert] = {}

    def upsert(self, alert: Alert) -> None:
        # dict preserves first-insertion order even when a key is re-assigned,
        # so open order is stable across extend/close updates.
        self._by_id[alert.alert_id] = alert

    def query(
        self,
        *,
        category: str | None = None,
        status: AlertStatus | str | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        want_status = _status_value(status)
        out: list[Alert] = []
        for a in self._by_id.values():
            if category is not None and a.category != category:
                continue
            if want_status is not None and a.status.value != want_status:
                continue
            out.append(a)
        if limit is not None:
            out = out[: max(0, limit)]
        return out

    def clear(self) -> None:
        self._by_id.clear()

    def close(self) -> None:  # nothing to release
        pass


# ------------------------------------------------------------------------ sqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id          TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    status            TEXT NOT NULL,
    detected_at       TEXT,
    window_start      TEXT,
    window_end        TEXT,
    anomaly_score     REAL NOT NULL,
    threshold         REAL NOT NULL,
    is_anomaly        INTEGER NOT NULL,
    top_features      TEXT NOT NULL,
    window_count      INTEGER NOT NULL,
    severity          TEXT NOT NULL,
    opened_at         TEXT,
    closed_at         TEXT,
    affected_channels TEXT NOT NULL,
    reason            TEXT,
    staleness_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_status   ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_category ON alerts(category);
"""

_COLUMNS = (
    "alert_id, category, status, detected_at, window_start, window_end, "
    "anomaly_score, threshold, is_anomaly, top_features, window_count, severity, "
    "opened_at, closed_at, affected_channels, reason, staleness_seconds"
)

# ON CONFLICT ... DO UPDATE (not INSERT OR REPLACE) so the row's rowid is
# preserved on update -- keeping ORDER BY rowid == open order stable.
_UPSERT = f"""
INSERT INTO alerts ({_COLUMNS})
VALUES (:alert_id, :category, :status, :detected_at, :window_start, :window_end,
        :anomaly_score, :threshold, :is_anomaly, :top_features, :window_count,
        :severity, :opened_at, :closed_at, :affected_channels, :reason,
        :staleness_seconds)
ON CONFLICT(alert_id) DO UPDATE SET
    category=excluded.category, status=excluded.status,
    detected_at=excluded.detected_at, window_start=excluded.window_start,
    window_end=excluded.window_end, anomaly_score=excluded.anomaly_score,
    threshold=excluded.threshold, is_anomaly=excluded.is_anomaly,
    top_features=excluded.top_features, window_count=excluded.window_count,
    severity=excluded.severity, opened_at=excluded.opened_at,
    closed_at=excluded.closed_at, affected_channels=excluded.affected_channels,
    reason=excluded.reason, staleness_seconds=excluded.staleness_seconds
"""


def _alert_to_params(a: Alert) -> dict:
    return {
        "alert_id": a.alert_id,
        "category": a.category,
        "status": a.status.value,
        "detected_at": a.detected_at,
        "window_start": a.window_start,
        "window_end": a.window_end,
        "anomaly_score": float(a.anomaly_score),
        "threshold": float(a.threshold),
        "is_anomaly": 1 if a.is_anomaly else 0,
        "top_features": json.dumps([[str(n), float(e)] for n, e in a.top_features]),
        "window_count": int(a.window_count),
        "severity": a.severity.value,
        "opened_at": a.opened_at,
        "closed_at": a.closed_at,
        "affected_channels": json.dumps(list(a.affected_channels)),
        "reason": a.reason,
        "staleness_seconds": a.staleness_seconds,
    }


def _row_to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        alert_id=row["alert_id"],
        category=row["category"],
        status=AlertStatus(row["status"]),
        detected_at=row["detected_at"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        anomaly_score=row["anomaly_score"],
        threshold=row["threshold"],
        is_anomaly=bool(row["is_anomaly"]),
        top_features=[(str(n), float(e)) for n, e in json.loads(row["top_features"])],
        window_count=int(row["window_count"]),
        severity=Severity(row["severity"]),
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        affected_channels=list(json.loads(row["affected_channels"])),
        reason=row["reason"],
        staleness_seconds=row["staleness_seconds"],
    )


class SQLiteAlertStore:
    """Durable :class:`AlertStore` backed by a single local SQLite file.

    The file (and any missing parent directories) is created automatically. One
    connection is shared with a lock, so it is safe under the backend's threaded
    TestClient / uvicorn workers. Each write is one autocommitted single-row
    upsert -- atomic, no partial multi-row lifecycle state.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        if str(self._path) in ("", "."):
            raise AlertStoreError(f"invalid ALERT_DB_PATH: {db_path!r}")
        try:
            if self._path.parent and not self._path.parent.exists():
                self._path.parent.mkdir(parents=True, exist_ok=True)
            # autocommit (isolation_level=None): each upsert is durable immediately.
            self._conn = sqlite3.connect(
                str(self._path), check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            self._lock = threading.RLock()
            with self._lock:
                self._conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:  # pragma: no cover - env-specific
            raise AlertStoreError(f"could not open alert database {self._path}: {exc}") from exc

    @property
    def path(self) -> Path:
        return self._path

    def upsert(self, alert: Alert) -> None:
        try:
            with self._lock:
                self._conn.execute(_UPSERT, _alert_to_params(alert))
        except sqlite3.Error as exc:
            # Do NOT pretend the write succeeded -- surface it; the caller decides.
            logger.exception("failed to persist alert %s", alert.alert_id)
            raise AlertStoreError(f"failed to persist alert {alert.alert_id}: {exc}") from exc

    def query(
        self,
        *,
        category: str | None = None,
        status: AlertStatus | str | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        sql = f"SELECT {_COLUMNS} FROM alerts"
        clauses: list[str] = []
        params: list[object] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        want_status = _status_value(status)
        if want_status is not None:
            clauses.append("status = ?")
            params.append(want_status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rowid"          # rowid == open (first-insert) order
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, limit))
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise AlertStoreError(f"alert query failed: {exc}") from exc

        out: list[Alert] = []
        for row in rows:
            try:
                out.append(_row_to_alert(row))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                # A single corrupt row must not sink recovery: skip it, keep going.
                logger.warning(
                    "skipping unreadable alert row %r during load",
                    row["alert_id"] if "alert_id" in row.keys() else "<unknown>",
                )
        return out

    def clear(self) -> None:
        try:
            with self._lock:
                self._conn.execute("DELETE FROM alerts")
        except sqlite3.Error as exc:
            raise AlertStoreError(f"failed to clear alert store: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ------------------------------------------------------------------ configuration


def alert_store_from_env(env: dict | None = None) -> AlertStore:
    """Build the configured store from environment variables.

    * ``ALERT_STORAGE_BACKEND`` = ``memory`` (default) | ``sqlite``.
    * ``ALERT_DB_PATH`` = SQLite file path (default ``alerts.sqlite3`` in the CWD),
      used only when the backend is ``sqlite``.

    Defaulting to ``memory`` keeps unit tests and any un-configured caller on the
    exact pre-Phase-7 behaviour; durability is opt-in.
    """
    env = os.environ if env is None else env
    backend = env.get(_ENV_BACKEND, "memory").strip().lower()
    if backend in ("memory", "inmemory", "in-memory"):
        return InMemoryAlertStore()
    if backend in ("sqlite", "sqlite3"):
        path = env.get(_ENV_DB_PATH) or _DEFAULT_DB_PATH
        return SQLiteAlertStore(path)
    raise AlertStoreError(
        f"unknown {_ENV_BACKEND}={backend!r}; expected 'memory' or 'sqlite'"
    )
