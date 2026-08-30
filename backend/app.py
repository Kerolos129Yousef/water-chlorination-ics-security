"""FastAPI application: GET /health and POST /score over the TranAD detector.

The app holds a single stateless :class:`~ml.src.detector.TranADDetector` on
``app.state`` and keeps **no** per-request or streaming state. Each ``/score``
call is one self-contained ``30 x 45`` window; maintaining the rolling window is
the caller's job (Phase 2A ``RollingWindow``).

Run locally::

    uvicorn backend.app:app --reload      # then open http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from alerting import AlertEngine
from alerting.alert import Alert
from ml.src.detector import TranADDetector

from .schemas import (
    ActiveAlertResponse,
    AlertModel,
    FeatureError,
    HealthResponse,
    ScoreRequest,
    ScoreResponse,
    StatusResponse,
)

logger = logging.getLogger("backend.api")

MODEL_TYPE = "TranAD"

# Dashboard runs separately from the API in development, so cross-origin requests
# need explicit allow-listing. These are the local static-server origins only --
# NOT a wildcard. A production origin must be added deliberately, never silently.
DEV_CORS_ORIGINS = [
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]


def create_app(
    detector: TranADDetector | None = None,
    alert_engine: AlertEngine | None = None,
) -> FastAPI:
    """Build the API.

    ``detector`` may be injected (tests pass the shared, already-loaded detector,
    or a stub); if ``None`` it is lazily loaded from the default artifacts on the
    first request, so importing this module never costs a model load.

    ``alert_engine`` holds the in-memory alert lifecycle state (Phase 4). One is
    created per app if not injected; tests inject a fresh one for isolation. The
    backend never re-implements dedup/lifecycle/severity -- it only drives and
    reads this engine. Alert state is in-memory and is lost on process restart.
    """
    app = FastAPI(
        title="Water Chlorination ICS — TranAD Detection API",
        version="0.1.0",
        summary="Thin API over the existing TranAD anomaly detector. "
        "The client owns the rolling 30-sample window; the detector is stateless.",
    )
    app.state.detector = detector
    app.state.alert_engine = alert_engine if alert_engine is not None else AlertEngine()
    app.state.last_detection = None  # rollup of the most recent /score, for /status

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request, exc: RequestValidationError):
        # Return a clean 422 carrying only location + message. We deliberately
        # drop the echoed raw ``input``: FastAPI's default handler includes it,
        # but a non-finite value there is not JSON-serialisable (Starlette uses
        # allow_nan=False) and would turn a 422 into a 500 -- and we never leak
        # internal request contents back to the client regardless.
        detail = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", "invalid input")}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    def get_detector() -> TranADDetector:
        det = app.state.detector
        if det is None:
            det = TranADDetector.from_artifacts()
            app.state.detector = det
        return det

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health(response: Response) -> HealthResponse:
        """Liveness + readiness of the TranAD detector.

        Returns **200** with ``detector_loaded=True`` when the detector is loaded
        (or can be lazily loaded on this call). If loading fails, returns **503**
        with ``status="unavailable"`` and ``detector_loaded=False`` -- an actual
        load failure is reported honestly rather than masked as ``ok`` or surfaced
        as an opaque 500. The real cause is logged server-side; no path or
        traceback is returned to the client.
        """
        try:
            det = get_detector()
        except Exception:  # noqa: BLE001 - any load failure => degraded, not a 500
            logger.exception("TranAD detector failed to load; reporting degraded /health")
            response.status_code = 503
            return HealthResponse(
                status="unavailable",
                detector_loaded=False,
                model_type=MODEL_TYPE,
                detail="TranAD detector is not loaded",
            )
        return HealthResponse(
            status="ok",
            detector_loaded=True,
            model_type=MODEL_TYPE,
            window=det.window,
            n_features=det.n_features,
            threshold=det.threshold,
            threshold_metadata=det.threshold_metadata,
            threshold_caveat=det.threshold_caveat,
        )

    @app.post("/score", response_model=ScoreResponse, tags=["detection"])
    def score(
        request: ScoreRequest,
        detector: TranADDetector = Depends(get_detector),
    ) -> ScoreResponse:
        """Score one complete ``30 x 45`` telemetry window.

        Shape/finiteness/duplicate checks happen in the request schema (422).
        Here we enforce feature *identity and order* against the model's canonical
        names, then delegate scoring to the existing detector unchanged.
        """
        canonical = list(detector.feature_names)
        _require_canonical_features(list(request.feature_names), canonical)

        # Schema already guaranteed 30 rows, each len == len(feature_names) == 45,
        # all finite; so this is a clean (30, 45) float64 array.
        window = np.asarray(request.window, dtype=np.float64)

        try:
            result = detector.score(window)
        except Exception:  # noqa: BLE001 - deliberately broad: convert to a clean 500
            # Log the full detail server-side; never return a traceback to the client.
            logger.exception("TranAD inference failed for a valid-shaped window")
            raise HTTPException(status_code=500, detail="inference failed")

        # Update monitoring/alert state from the same decision. This must never
        # affect the detection response: a failure here is logged and swallowed so
        # /score keeps returning the score even if the monitoring side has a bug.
        _update_monitoring(app, request, result)

        return ScoreResponse(
            anomaly_score=float(result.anomaly_score),
            threshold=float(result.threshold),
            is_anomaly=bool(result.is_anomaly),
            feature_errors=[
                FeatureError(feature=name, error=float(err))
                for name, err in zip(canonical, result.feature_errors)
            ],
            feature_names=canonical,
            window_start=request.window_start,
            window_end=request.window_end,
        )

    # ---------------------------------------------------------------- monitoring

    @app.get("/status", response_model=StatusResponse, tags=["monitoring"])
    def status() -> StatusResponse:
        """System + detector + latest-detection + alert rollup for the dashboard.

        Always 200: if the detector cannot load, reports ``status="degraded"`` /
        ``detector_loaded=False`` with detector fields null (``/health`` stays the
        503 liveness probe). Alert state is in-memory; it resets on restart.
        """
        engine: AlertEngine = app.state.alert_engine
        last = app.state.last_detection
        try:
            det = get_detector()
            det_fields = dict(
                detector_loaded=True,
                window=det.window,
                n_features=det.n_features,
                threshold=det.threshold,
                threshold_caveat=det.threshold_caveat,
            )
            overall = "ok"
        except Exception:  # noqa: BLE001 - degraded, not a 500; dashboard still renders
            logger.exception("detector unavailable while building /status")
            det_fields = dict(
                detector_loaded=False,
                window=None,
                n_features=None,
                threshold=None,
                threshold_caveat=None,
            )
            overall = "degraded"

        return StatusResponse(
            status=overall,
            model_type=MODEL_TYPE,
            last_anomaly_score=None if last is None else last["anomaly_score"],
            last_is_anomaly=None if last is None else last["is_anomaly"],
            last_window_end=None if last is None else last["window_end"],
            active_alert_count=1 if engine.active_alert is not None else 0,
            total_alert_count=len(engine.alerts),
            **det_fields,
        )

    @app.get("/alerts", response_model=list[AlertModel], tags=["monitoring"])
    def list_alerts(limit: int = 50, status: str | None = None) -> list[AlertModel]:
        """Alert history, most-recent first. Optional ``status=open|closed`` filter."""
        alerts = list(app.state.alert_engine.alerts)
        if status is not None:
            wanted = status.upper()
            alerts = [a for a in alerts if a.status.value == wanted]
        alerts = list(reversed(alerts))[: max(0, limit)]
        return [_alert_to_model(a) for a in alerts]

    @app.get("/alerts/active", response_model=ActiveAlertResponse, tags=["monitoring"])
    def active_alert() -> ActiveAlertResponse:
        """The currently-open alert, or ``null`` when the stream is normal."""
        active = app.state.alert_engine.active_alert
        return ActiveAlertResponse(
            active_alert=_alert_to_model(active) if active is not None else None
        )

    return app


def _update_monitoring(app: FastAPI, request: ScoreRequest, result) -> None:
    """Feed one detection decision into the alert engine + record the last score.

    Deliberately best-effort: monitoring must never break detection, so any failure
    is logged and swallowed. Timestamps come from the request (the detector never
    sees them); they are ISO strings so the alert record is JSON-serialisable.
    """
    try:
        ws = request.window_start.isoformat() if request.window_start else None
        we = request.window_end.isoformat() if request.window_end else None
        app.state.alert_engine.process(result, window_start=ws, window_end=we)
        app.state.last_detection = {
            "anomaly_score": float(result.anomaly_score),
            "is_anomaly": bool(result.is_anomaly),
            "threshold": float(result.threshold),
            "window_end": we,
        }
    except Exception:  # noqa: BLE001 - monitoring failure must not affect /score
        logger.exception("monitoring/alert update failed; detection response unaffected")


def _alert_to_model(alert: Alert) -> AlertModel:
    """Map an :class:`~alerting.alert.Alert` to its stable wire DTO."""
    return AlertModel(
        alert_id=alert.alert_id,
        category=alert.category,
        status=alert.status.value,
        severity=alert.severity.value,
        is_anomaly=alert.is_anomaly,
        anomaly_score=alert.anomaly_score,
        threshold=alert.threshold,
        detected_at=alert.detected_at,
        window_start=alert.window_start,
        window_end=alert.window_end,
        opened_at=alert.opened_at,
        closed_at=alert.closed_at,
        window_count=alert.window_count,
        top_features=[FeatureError(feature=name, error=err) for name, err in alert.top_features],
    )


def _require_canonical_features(provided: list[str], canonical: list[str]) -> None:
    """Raise 422 unless ``provided`` equals ``canonical`` exactly (names and order).

    Distinct messages for the distinct failure modes the API contract calls out.
    Reordering is rejected rather than silently corrected, so a caller can never
    feed columns to the model in the wrong order without noticing.
    """
    if provided == canonical:
        return

    provided_set, canonical_set = set(provided), set(canonical)
    unknown = sorted(provided_set - canonical_set)
    missing = sorted(canonical_set - provided_set)

    if unknown:
        detail = f"unknown feature(s) not in the model contract: {unknown}"
    elif missing:
        detail = f"missing required feature(s): {missing}"
    else:
        # Same set of names, wrong order.
        detail = (
            "feature_names must be in the model's canonical order; "
            "silent reordering is not allowed"
        )
    raise HTTPException(status_code=422, detail=detail)


# Module-level app for `uvicorn backend.app:app`. Detector loads lazily on the
# first request, so import stays cheap.
app = create_app()
