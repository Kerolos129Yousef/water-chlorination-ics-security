# `backend/` — TranAD Detection API (Phase 2B)

A **thin** FastAPI boundary over the existing, stateless `TranADDetector`. It
validates requests, delegates to `ml.src`, and shapes the response. That is all.

```
client ──▶ FastAPI (validate) ──▶ ml.src.detector.TranADDetector ──▶ DetectionResult ──▶ JSON
```

**What this layer does NOT do** (by design):
- It does **not** own the rolling 30-sample buffer — that is
  `simulator.window_buffer.RollingWindow` (Phase 2A). The **caller** maintains the
  window and posts a complete `30 × 45` one.
- It keeps **no** streaming state between requests; each `/score` is independent.
- It does **not** run replay, and does **not** duplicate TranAD architecture,
  preprocessing, scoring, or threshold logic — all reused from `ml.src`.
- Dependency direction is strictly **`backend → ml.src`**; `ml.src` never imports
  `backend`, and stays usable without FastAPI installed.

## Run

```bash
pip install -r backend/requirements.txt
uvicorn backend.app:app --reload      # http://127.0.0.1:8000/docs  (OpenAPI UI)
```

Or run it containerized (Phase 8A) — same `uvicorn backend.app:app`, CPU-only,
non-root, with SQLite persisted to a volume:

```bash
docker compose up --build -d          # backend :8000, dashboard :8080
```

See `docs/provenance/phase8a_docker.md` for the image/hardening details.

## Endpoints

### `GET /health`
Liveness + readiness. The detector loads lazily on the first request:

- **200** when the detector is loaded (or lazily loads on this call): full
  diagnostics, `status="ok"`, `detector_loaded=true` (no secrets, no paths).
- **503** when detector loading fails: `status="unavailable"`,
  `detector_loaded=false`, detector-derived fields `null`, and a short
  `detail`. The real cause is logged server-side; no path or traceback is
  returned.

```json
{
  "status": "ok",
  "detector_loaded": true,
  "model_type": "TranAD",
  "window": 30,
  "n_features": 45,
  "threshold": 9.269035945180804e-05,
  "threshold_metadata": {"threshold": 9.26e-05, "percentile": 99, "metric": "tranad_score", "window": 30},
  "threshold_caveat": "threshold.json is the 99th percentile ... expect FPR > 1% ..."
}
```

### `POST /score`
**Request** — one complete window, features aligned to the canonical order:
```json
{
  "feature_names": ["FIT101", "LIT101", "...45 names in canonical order..."],
  "window": [[/* 45 values */], "...30 timesteps..."],
  "window_start": "2015-12-28T10:00:00",   // optional, echoed back
  "window_end":   "2015-12-28T10:02:25"    // optional, echoed back
}
```
`feature_names` must equal `ml/artifacts/swat_TranAD/feature_names.json` **exactly
(names and order)**. Timestamps are optional and pass straight through — the
detector never sees them.

An **optional** `telemetry_health` object may be attached (Phase 6B): the client's
per-window stuck-channel verdict, which the backend forwards to the alert engine's
telemetry track. It never affects scoring and the response is unchanged. Example:
```json
"telemetry_health": {
  "healthy": false, "fault_type": "TELEMETRY_FAULT",
  "timestamp": "2015-12-28T10:02:25",
  "stuck_channels": [{"feature": "FIT101", "unchanged_samples": 2880, "staleness_seconds": 14395.0}]
}
```

**Response**:
```json
{
  "anomaly_score": 0.00012,
  "threshold": 9.269035945180804e-05,
  "is_anomaly": true,
  "feature_errors": [{"feature": "FIT101", "error": 0.0031}, "...45..."],
  "feature_names": ["FIT101", "...45..."],
  "window_start": null,
  "window_end": null
}
```
No severity or high/medium/low classification — just the score, the threshold,
the boolean decision, and the per-feature contribution.

Each `/score` call also updates the in-memory alert engine (Phase 4) — the
detection decision on the `PROCESS_ANOMALY` track, and the optional
`telemetry_health` verdict on the `TELEMETRY_FAULT` track (Phase 6B). This does
**not** change the `/score` response.

## Monitoring endpoints (Phase 5A)

Read-only views over the `AlertEngine` on `app.state`. The backend only drives and
reads the engine — deduplication, lifecycle, severity, and top-feature attribution
all live in `alerting/`, never here.

**Persistence (Phase 7).** Alert state is durable *behind* the engine via an
`AlertStore`. Configure with `ALERT_STORAGE_BACKEND=memory|sqlite` (default
`memory`) and `ALERT_DB_PATH=…` (SQLite file; auto-created). With `sqlite`, a
backend restart recovers active incidents (both categories) and history — the API
serves the recovered state. The default (`memory`) keeps the old behaviour (state
lost on restart). FastAPI itself remains stateless; runtime `*.sqlite3` files are
git-ignored. See `docs/provenance/phase7_alert_persistence.md`.

### `GET /status`
System + detector + latest-detection + alert rollup. Always **200** (reports
`status="degraded"`, `detector_loaded=false` if the detector cannot load — unlike
`/health`, which is the 503 liveness probe):
```json
{
  "status": "ok", "detector_loaded": true, "model_type": "TranAD",
  "window": 30, "n_features": 45, "threshold": 9.269035945180804e-05,
  "threshold_caveat": "...",
  "last_anomaly_score": 0.0021, "last_is_anomaly": true, "last_window_end": "2015-12-30T09:51:05",
  "active_alert_count": 1, "total_alert_count": 3,
  "active_telemetry_fault_count": 1, "telemetry_fault_channels": ["FIT101"],
  "alert_state_in_memory": true
}
```
`active_alert_count` is the ML track; `active_telemetry_fault_count` /
`telemetry_fault_channels` are the Phase 6B telemetry rollup (bounded summary, no
per-sample state).

### `GET /alerts?limit=50&status=open|closed&category=PROCESS_ANOMALY|TELEMETRY_FAULT`
Alert history, **most-recent first**. Optional `status` and `category` filters
(the latter lets the UI list the two kinds of finding separately). Returns a list
of the stable `AlertModel` DTO — which carries `category`, and for telemetry faults
`affected_channels`, `reason`, and `staleness_seconds`.

### `GET /alerts/active`
The currently-open **PROCESS_ANOMALY** alert, or `null` when normal (ML track only;
telemetry faults are read via `/alerts?category=TELEMETRY_FAULT` and the `/status`
rollup):
```json
{
  "active_alert": {
    "alert_id": "alert-0001-2015-12-30T09:51:05", "category": "PROCESS_ANOMALY",
    "status": "OPEN", "severity": "HIGH", "is_anomaly": true,
    "anomaly_score": 0.0021, "threshold": 9.269035945180804e-05,
    "detected_at": "2015-12-30T09:51:05", "window_start": "2015-12-30T09:51:00",
    "window_end": "2015-12-30T09:51:05", "opened_at": "2015-12-30T09:51:05",
    "closed_at": null, "window_count": 1,
    "top_features": [{"feature": "AIT201", "error": 0.0253}, "...5..."]
  }
}
```
`severity` is an **engineering heuristic** (`score ÷ threshold` band), not an ML
output.

## CORS

The dashboard runs on a separate origin in development, so the backend allow-lists
the dev static-server origins explicitly (`DEV_CORS_ORIGINS` in `app.py`) — **not**
a wildcard. A production origin must be added deliberately.

## Error behaviour

| Situation | Status |
|---|---|
| < 30 or > 30 timesteps, wrong value count per row, non-finite value, duplicate feature name, unknown top-level field, malformed JSON, wrong types | **422** |
| Unknown / missing feature name, or features not in canonical order | **422** |
| Valid request but inference raises | **500** `{"detail": "inference failed"}` |

A scoring failure returns a controlled 500. Python tracebacks are never sent to
the client (they are logged server-side).

The two 422 paths carry **deliberately distinct `detail` shapes**:

- **A. Schema / Pydantic validation** (wrong timestep count, ragged rows,
  non-finite value, duplicate name, unknown top-level field, malformed JSON,
  wrong types) → `detail` is a **structured list** of
  `{"loc": [...], "msg": "..."}` entries (raw echoed `input` is stripped).
- **B. Feature identity / order contract** (unknown feature, missing feature,
  features not in canonical order) → `detail` is a **single human-readable
  string** naming the specific violation.

## Tests

- `tests/test_api.py` — 22 cases (health 200 + lazy-load path + 503-on-load-failure,
  valid score, the rejection matrix across both 422 forms, determinism, threshold
  decision, per-feature attribution, controlled 500, detector-actually-invoked, no
  artifact mutation), reusing the Phase 1 golden vectors.
- `tests/test_monitoring_api.py` — 15 cases for `/status`, `/alerts`, `/alerts/active`:
  active alert appears after an anomaly and closes on return to normal, dedup of
  consecutive anomalies, status filter, stable serialization, CORS (dev origin
  allowed, no wildcard), unchanged `/health` + `/score` contracts, and an end-to-end
  replay → `/score` → engine → `/alerts/active` test.

No dataset required. Part of the full **286-test** suite (`pytest -q`).
