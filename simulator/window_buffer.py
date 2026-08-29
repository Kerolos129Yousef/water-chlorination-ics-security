"""Rolling window buffer: telemetry records in, fixed ``(window, features)`` windows out.

This is the component that *owns the rolling buffer* (Phase 2A, buffer-ownership
decision). The TranAD detector is stateless and expects a fully-formed raw
``(30, 45)`` window; something has to hold the last 30 samples and hand one over
each time a new sample arrives. That owner is :class:`RollingWindow`.

Deliberately independent of both TranAD and the replay engine:

* **No TranAD.** It emits *raw* telemetry windows. Scaling, scoring and the
  threshold decision belong to :mod:`ml.src.detector`. This buffer never imports
  torch and never looks at the model.
* **No replay.** It consumes any object exposing ``.values`` / ``.timestamp`` /
  ``.label`` (duck-typed), so it works with :class:`~simulator.swat_replay.TelemetryRecord`
  but is not coupled to it. A live sensor feed could push the same shape.

Contract (matches the offline ``ml.src.dataset.sliding_windows`` semantics so the
online and batch paths agree): ``window`` samples per window, stride 1, no output
until the buffer is full, first output on the ``window``-th push, one new window
per push thereafter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol

import numpy as np

__all__ = ["RollingWindow", "Window", "TelemetryLike", "WindowBufferError"]

# The verified SWaT/TranAD contract (provenance §4). Defaults, not hard-codes:
# the buffer is parameterised so it stays a general component.
DEFAULT_WINDOW = 30
DEFAULT_N_FEATURES = 45


class WindowBufferError(ValueError):
    """Raised on a malformed telemetry record (wrong feature count, non-finite)."""


class TelemetryLike(Protocol):
    """The minimal shape :meth:`RollingWindow.push` needs.

    ``label`` is optional; a source without ground truth (a live feed) simply
    omits it and the buffer records 0.
    """

    timestamp: np.datetime64
    values: np.ndarray


@dataclass(frozen=True)
class Window:
    """One fully-formed rolling window, ready for :meth:`TranADDetector.score`.

    Attributes
    ----------
    values:
        ``(window, n_features)`` float64 raw telemetry in ``feature_names`` order.
        A fresh array -- it does not alias the buffer's internal storage, so a
        consumer may keep or mutate it without corrupting the next window.
    timestamps:
        ``(window,)`` datetime64[s], oldest → newest. Carried so a dashboard can
        show *when* an anomaly occurred (Phase 2A requirement §6).
    feature_names:
        Feature order, so a window is self-describing.
    labels:
        ``(window,)`` int8 ground truth, 1 = attack. Metadata only -- the buffer
        never uses it. 0 when the source provides none.
    """

    values: np.ndarray
    timestamps: np.ndarray
    feature_names: tuple[str, ...]
    labels: np.ndarray = field(repr=False)

    def __len__(self) -> int:
        return len(self.values)

    @property
    def last_timestamp(self) -> np.datetime64:
        """Timestamp of the sample TranAD actually scores (window's final step)."""
        return self.timestamps[-1]

    @property
    def label(self) -> int:
        """Last-timestep label -- the convention that matches TranAD scoring.

        TranAD reconstructs only ``src[-1:]``, so a window is a statement about
        its final sample. This mirrors ``ml.src.metrics.window_labels_last``; it
        is *not* "any attack in the window".
        """
        return int(self.labels[-1])

    @property
    def contains_attack(self) -> bool:
        """True if any sample in the window is labelled attack (any-in-window)."""
        return bool(self.labels.max() > 0)


class RollingWindow:
    """Owns the last ``window`` telemetry samples and emits ``(window, n_features)``.

    Usage::

        buf = RollingWindow(feature_names)          # 45 names from feature_names.json
        for record in source:
            window = buf.push(record)               # None until 30 samples seen
            if window is not None:
                result = detector.score(window.values)

    or, over an iterable::

        for window in buf.stream(source):
            ...
    """

    def __init__(
        self,
        feature_names: Iterable[str],
        window: int = DEFAULT_WINDOW,
    ) -> None:
        self._feature_names = tuple(feature_names)
        self._n_features = len(self._feature_names)
        if self._n_features < 1:
            raise WindowBufferError("feature_names must be non-empty")
        if window < 1:
            raise WindowBufferError(f"window must be >= 1, got {window}")
        self._window = window
        # maxlen makes eviction automatic: the (window+1)-th push drops the
        # oldest sample, which is exactly stride-1 advancement. The deque IS the
        # rolling buffer -- the piece of owned state this whole component exists for.
        self._values: deque[np.ndarray] = deque(maxlen=window)
        self._timestamps: deque[np.datetime64] = deque(maxlen=window)
        self._labels: deque[int] = deque(maxlen=window)
        self._n_seen = 0

    # --------------------------------------------------------------- properties

    @property
    def window(self) -> int:
        return self._window

    @property
    def n_features(self) -> int:
        return self._n_features

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self._feature_names

    @property
    def is_full(self) -> bool:
        """True once at least ``window`` samples have been buffered."""
        return len(self._values) == self._window

    @property
    def n_seen(self) -> int:
        """Total samples pushed since construction or last :meth:`reset`."""
        return self._n_seen

    # ------------------------------------------------------------------ pushing

    def push(self, record: TelemetryLike) -> Window | None:
        """Add one telemetry sample; return a :class:`Window` once full, else None.

        The input record is never mutated: ``values`` is validated and copied
        before it enters the buffer, so a caller reusing one array across pushes
        (or mutating it afterwards) cannot corrupt buffered history.
        """
        values = np.asarray(record.values, dtype=np.float64)
        if values.shape != (self._n_features,):
            raise WindowBufferError(
                f"expected {self._n_features} features, got shape {values.shape}. "
                f"The record does not match the {self._n_features}-feature contract "
                f"this buffer was built for."
            )
        if not np.isfinite(values).all():
            bad = np.flatnonzero(~np.isfinite(values))
            names = [self._feature_names[i] for i in bad[:5]]
            raise WindowBufferError(
                f"non-finite value(s) in features {names} "
                f"({len(bad)} total). A stuck or absent channel is an operational "
                f"fault; this buffer does not impute (see provenance §5.2)."
            )

        # copy(): decouple buffered history from the caller's array.
        self._values.append(values.copy())
        self._timestamps.append(np.datetime64(record.timestamp, "s"))
        self._labels.append(int(getattr(record, "label", 0)))
        self._n_seen += 1

        if len(self._values) < self._window:
            return None
        # np.stack allocates a fresh (window, n_features) array -- the returned
        # Window owns its data, independent of the deque it was built from.
        return Window(
            values=np.stack(self._values),
            timestamps=np.array(self._timestamps, dtype="datetime64[s]"),
            feature_names=self._feature_names,
            labels=np.array(self._labels, dtype=np.int8),
        )

    def stream(self, records: Iterable[TelemetryLike]) -> Iterator[Window]:
        """Yield one :class:`Window` per record once the buffer is full.

        For ``n`` records this yields ``max(0, n - window + 1)`` windows. Lazy:
        holds one window at a time, so replaying the whole dataset never
        materialises all windows at once.
        """
        for record in records:
            window = self.push(record)
            if window is not None:
                yield window

    def reset(self) -> None:
        """Drop all buffered samples. The next full window needs ``window`` pushes again."""
        self._values.clear()
        self._timestamps.clear()
        self._labels.clear()
        self._n_seen = 0
