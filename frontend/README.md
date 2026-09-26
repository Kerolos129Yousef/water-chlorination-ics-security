# `frontend/` — Operator Dashboard (Phase 5A)

A **minimal, zero-build** operator dashboard: a single `index.html` (vanilla
HTML/CSS/JS, no framework, no npm). It talks **only** to the backend API by
polling — it contains no detection or alert logic.

```
frontend (poll) ──▶ backend API (/status, /alerts/active, /alerts[?category=…]) ──▶ AlertEngine + TranAD state
```

## Run (dashboard served separately from the API — the dev model)

```bash
# terminal 1 — backend API
uvicorn backend.app:app                       # http://127.0.0.1:8000

# terminal 2 — static dashboard
cd frontend && python -m http.server 5500     # open http://127.0.0.1:5500/
```

Because the dashboard origin (`:5500`) differs from the API origin (`:8000`), the
backend allow-lists the dev static-server origins via CORS (explicit list, not a
wildcard — see `backend/app.py: DEV_CORS_ORIGINS`).

Drive some data through it with the Phase 3 demo against the same backend:

```bash
python scripts/run_e2e_demo.py --mode attack --base-url http://127.0.0.1:8000
```

### Configuration (URL query params)

| Param | Default | Meaning |
|---|---|---|
| `api` | `http://127.0.0.1:8000` | Backend base URL |
| `poll` | `2000` | Poll interval in ms |

e.g. `http://127.0.0.1:5500/?api=http://127.0.0.1:8000&poll=1000`

## What it shows

1. **System status** — backend status, detector loaded/unavailable, model = TranAD, threshold, and a **telemetry-fault rollup** (`N active` + stuck channel names, or `healthy`).
2. **Detection status** — latest window's anomaly score, threshold, NORMAL/ANOMALY decision, window end, plus a small score sparkline.
3. **Active process anomaly** — id, status, heuristic severity, peak score, threshold, opened time, window span, window count, top contributing features.
4. **Telemetry / sensor faults** (Phase 6B) — each active `TELEMETRY_FAULT` with its affected channel(s), how long it has been frozen, observation count, opened time, and reason. A stuck sensor is shown as a **distinct category**, never as an ML anomaly.
5. **Process-anomaly history** and **Telemetry-fault history** — the unified `/alerts` list split by `category` so each track reads cleanly (status, channels/severity, times).

## Live updates

Simple **polling** (default 2 s) — no WebSockets/SSE. Sufficient for the MVP.

## Graceful states

- **Backend unavailable** → a red banner + "backend unavailable" pill; the last
  render is kept and polling retries.
- **No active alerts / no history** → explicit empty-state text.
- **Detector unavailable** → status shows `degraded` / detector "unavailable".
- API values are escaped before rendering; no raw tracebacks are ever shown.

## Limitations (this phase)

Alert state is **in-memory in the backend** and resets when that process restarts;
the dashboard reflects whatever the backend currently holds. No database,
authentication, or notifications in this phase.
