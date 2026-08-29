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

`tests/test_api.py` — 22 cases (health 200 + lazy-load path + 503-on-load-failure,
valid score, the rejection matrix across both 422 forms, determinism, threshold
decision, per-feature attribution, controlled 500, detector-actually-invoked, no
artifact mutation), reusing the Phase 1 golden vectors. No dataset required. Part
of the full **238-test** suite (`pytest -q`).
