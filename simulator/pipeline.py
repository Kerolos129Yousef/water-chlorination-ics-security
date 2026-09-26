"""End-to-end pipeline glue: replay + rolling window → HTTP /score → result.

Phase 3, the integration that wires the already-built pieces together and proves
the whole path works on real SWaT data::

    SWaTReplay ─▶ RollingWindow ─▶ window_to_request ─▶ POST /score ─▶ TranAD ─▶ EndToEndResult
     (this file orchestrates the producer/client side; it does NOT re-implement any of them)

What this module is, and is not
-------------------------------
* It is the **producer/client** side. It drives the replay, feeds the rolling
  window, serialises each full ``30 x 45`` window into the ``/score`` request
  body, hands it to a *scorer*, and shapes the JSON reply into
  :class:`EndToEndResult`.
* It owns **no** detection logic, **no** buffering, and **no** transport of its
  own. The 30-sample buffer stays in :class:`~simulator.window_buffer.RollingWindow`;
  scoring stays behind the FastAPI boundary in ``ml.src``. The transport is
  *injected* as a ``Scorer`` -- so this file imports neither ``torch``, nor
  ``fastapi``, nor ``ml.src`` (keeping the simulator layer free of both, exactly
  as in Phase 2A).
* The production transport is :class:`HttpScorer` (a real HTTP client against a
  running ``uvicorn``). Tests inject a ``TestClient``-backed callable that
  exercises the identical ASGI app in-process -- same validation, same
  serialisation, no socket and no waiting.

The window's own timestamps travel into the request and back out unchanged;
pacing (:data:`real_time_factor`) controls playback speed only, it never rewrites
time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Protocol

import numpy as np

from .swat_replay import SWaTReplay
from .telemetry_health import TelemetryHealthMonitor, TelemetryHealthResult
from .window_buffer import RollingWindow, Window

__all__ = [
    "window_to_request",
    "health_to_request_payload",
    "EndToEndResult",
    "Scorer",
    "HttpScorer",
    "run_pipeline",
]


def _iso(ts: np.datetime64) -> str:
    """datetime64[s] → an ISO-8601 string FastAPI's ``datetime`` field parses."""
    return str(np.datetime64(ts, "s"))


def window_to_request(window: Window) -> dict[str, Any]:
    """Map one rolling :class:`Window` to the ``POST /score`` JSON body.

    Pure and transport-free: ``feature_names`` in canonical order, the raw
    ``30 x 45`` matrix as nested lists, and the window's first/last real
    timestamps as ``window_start`` / ``window_end`` (the API echoes these back;
    the detector never sees them). No scaling, no scoring -- that is the server's
    job, reached through this body.
    """
    return {
        "feature_names": list(window.feature_names),
        "window": window.values.tolist(),
        "window_start": _iso(window.timestamps[0]),
        "window_end": _iso(window.last_timestamp),
    }


def health_to_request_payload(health: TelemetryHealthResult) -> dict[str, Any]:
    """Serialise a :class:`TelemetryHealthResult` into the ``/score`` request's
    optional ``telemetry_health`` field (Phase 6B).

    Pure and transport-free. Carries the verdict the backend forwards to the alert
    engine -- the client computes it (the monitor is a per-sample stream observer),
    the API only relays it. The ML input is untouched.
    """
    return {
        "healthy": bool(health.healthy),
        "fault_type": health.fault_type,
        "reason": health.reason,
        "timestamp": None if health.timestamp is None else _iso(health.timestamp),
        "stuck_channels": [
            {
                "feature": ch.feature,
                "unchanged_samples": int(ch.unchanged_samples),
                "staleness_seconds": ch.staleness_seconds,
                "last_value": float(ch.last_value),
                "reason": ch.reason,
            }
            for ch in health.stuck_channels
        ],
    }


@dataclass(frozen=True)
class EndToEndResult:
    """One window's trip through the full pipeline, as data.

    Carries exactly what the flow must preserve (Phase 3 §7): the window bounds,
    the detector's decision, and the 45-way per-feature attribution -- nothing
    invented. ``ground_truth_label`` / ``contains_attack`` are copied from the
    source window's labels purely so a demo can *measure* detection; they are
    metadata, never a model output and never an input to the decision.
    """

    window_start: str | None
    window_end: str | None
    anomaly_score: float
    threshold: float
    is_anomaly: bool
    feature_errors: tuple[tuple[str, float], ...]
    feature_names: tuple[str, ...] = field(repr=False)
    ground_truth_label: int = 0
    contains_attack: bool = False
    telemetry_health: TelemetryHealthResult | None = None

    def top_features(self, n: int = 5) -> list[tuple[str, float]]:
        """The ``n`` largest per-feature errors, descending -- which sensors drove it."""
        return sorted(self.feature_errors, key=lambda kv: kv[1], reverse=True)[:n]


def _result_from(
    response: Mapping[str, Any],
    window: Window,
    telemetry_health: TelemetryHealthResult | None = None,
) -> EndToEndResult:
    """Build an :class:`EndToEndResult` from a ``/score`` reply + the source window.

    ``telemetry_health`` (when a monitor is wired in) is the sensor-health verdict
    for this window's final sample. It is carried *alongside* the ML decision, kept
    strictly separate: an ML anomaly and a telemetry fault can both be present and
    stay distinguishable.
    """
    feature_errors = tuple(
        (fe["feature"], float(fe["error"])) for fe in response["feature_errors"]
    )
    return EndToEndResult(
        window_start=response.get("window_start"),
        window_end=response.get("window_end"),
        anomaly_score=float(response["anomaly_score"]),
        threshold=float(response["threshold"]),
        is_anomaly=bool(response["is_anomaly"]),
        feature_errors=feature_errors,
        feature_names=tuple(response["feature_names"]),
        ground_truth_label=window.label,          # last-timestep label (metadata only)
        contains_attack=window.contains_attack,   # any-in-window (metadata only)
        telemetry_health=telemetry_health,
    )


class Scorer(Protocol):
    """Anything that turns a ``/score`` request body into the parsed reply dict.

    The whole point of the injection: production passes :class:`HttpScorer` (real
    HTTP); tests pass a ``TestClient``-backed callable. Either way this module
    stays ignorant of the transport and of the detector behind it.
    """

    def __call__(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...


class HttpScorer:
    """POST a window body to a live FastAPI ``/score`` and return the JSON reply.

    The real production boundary: an HTTP round-trip to a running ``uvicorn``.
    ``httpx`` is imported lazily so importing :mod:`simulator.pipeline` never
    requires an HTTP stack (tests that inject their own scorer pull in nothing).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        client: Any | None = None,
        timeout: float = 10.0,
        path: str = "/score",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.path = path
        if client is None:
            import httpx  # lazy: only needed for real HTTP transport

            client = httpx.Client(base_url=self.base_url, timeout=timeout)
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = client

    def __call__(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        resp = self._client.post(self.path, json=dict(body))
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpScorer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def run_pipeline(
    replay: SWaTReplay,
    scorer: Scorer,
    *,
    start: int = 0,
    stop: int | None = None,
    window: int = 30,
    real_time_factor: float = math.inf,
    sleeper: Callable[[float], None] | None = None,
    buffer: RollingWindow | None = None,
    health_monitor: TelemetryHealthMonitor | None = None,
) -> Iterator[EndToEndResult]:
    """Stream ``replay[start:stop]`` through the full pipeline, one result per window.

    For ``n`` streamed records this yields ``max(0, n - window + 1)`` results:
    nothing until the rolling buffer is full (the 30-sample warm-up), then one
    per subsequent sample (stride 1) -- the buffering contract is
    :class:`RollingWindow`'s, reused verbatim.

    ``health_monitor`` is optional and non-invasive. When supplied, every raw
    sample is *also* observed by the :class:`TelemetryHealthMonitor` (independently
    of the buffer), and each emitted result carries the sensor-health verdict for
    its final sample in ``EndToEndResult.telemetry_health``. The monitor never
    alters the record or the window fed to TranAD -- the ML path is byte-identical
    whether or not a monitor is present.

    Pacing is delegated to :meth:`SWaTReplay.stream`. The default
    ``real_time_factor=inf`` means *no wait* -- correct for tests and
    as-fast-as-possible batch runs. A demo passes a finite factor (e.g. 60) to
    play back faster-than-live while still paced; the injected ``sleeper`` lets a
    test assert the intended delays without ever really sleeping.
    """
    buf = buffer if buffer is not None else RollingWindow(replay.feature_names, window=window)
    for record in replay.stream(
        start=start, stop=stop, real_time_factor=real_time_factor, sleeper=sleeper
    ):
        # Observe health on the same raw sample the buffer sees; observation is
        # read-only and independent of buffering/scoring.
        health = health_monitor.observe(record) if health_monitor is not None else None
        win = buf.push(record)
        if win is None:
            continue  # warm-up: buffer not yet full, no window to score
        body = window_to_request(win)
        if health is not None:
            # Attach the window's health verdict so the backend can surface a
            # TELEMETRY_FAULT alongside (never merged into) the ML decision.
            body["telemetry_health"] = health_to_request_payload(health)
        response = scorer(body)
        yield _result_from(response, win, telemetry_health=health)
