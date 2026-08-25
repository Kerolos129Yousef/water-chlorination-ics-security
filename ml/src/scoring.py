"""TranAD anomaly scoring.

Reproduces ``tranad_score`` from
``research/notebooks/swat/SWaT_Anomaly_Detection.ipynb`` (cell 15) exactly. No
new score is invented here: the scalar returned by :func:`anomaly_scores` is the
same quantity the shipped threshold in ``threshold.json`` was calibrated
against, so any change to this formula invalidates that threshold.

The research implementation::

    src  = wb.permute(1, 0, 2)      # (W, B, F)
    elem = src[-1:, :, :]           # (1, B, F) -- the last timestep
    x1, x2 = model(src, elem)
    s = 0.5*((x1-elem)**2).mean(dim=2) + 0.5*((x2-elem)**2).mean(dim=2)

Because TranAD reconstructs only the last timestep, taking the squared error
*before* the feature-dimension mean yields a 45-length per-feature error vector
-- i.e. which sensors drove the score. That is exposed by
:func:`per_feature_errors` at no extra cost and is the evidence behind the
project's "process-aware detection" claim.
"""

from __future__ import annotations

import numpy as np
import torch

__all__ = [
    "reconstruct",
    "anomaly_scores",
    "per_feature_errors",
    "score_windows",
]


def _to_sequence_first(windows: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """(B, W, F) -> (src=(W, B, F), elem=(1, B, F))."""
    if windows.ndim != 3:
        raise ValueError(
            f"expected (batch, window, n_features), got shape {tuple(windows.shape)}"
        )
    src = windows.permute(1, 0, 2)
    elem = src[-1:, :, :]
    return src, elem


@torch.no_grad()
def reconstruct(
    model: torch.nn.Module, windows: torch.Tensor
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Run both TranAD decoders.

    Returns ``(x1, x2, elem)``, each ``(1, B, F)``. Both reconstructions are
    returned deliberately -- the score is an equal-weight blend of the two, so
    collapsing them here would hide half the model.
    """
    src, elem = _to_sequence_first(windows)
    x1, x2 = model(src, elem)
    return x1, x2, elem


def anomaly_scores(
    x1: torch.Tensor, x2: torch.Tensor, elem: torch.Tensor
) -> torch.Tensor:
    """Scalar score per window, ``(B,)``.

    Written as the exact expression from the notebook (mean over the feature dim
    applied to each reconstruction *before* the 0.5/0.5 blend) to preserve
    bit-level agreement with the research run. Algebraically identical to
    ``per_feature_errors(...).mean(-1)``, but floating-point summation order
    differs, so the two agree to ~1e-7 rather than exactly.
    """
    s = 0.5 * ((x1 - elem) ** 2).mean(dim=2) + 0.5 * ((x2 - elem) ** 2).mean(dim=2)
    return s.squeeze(0)


def per_feature_errors(
    x1: torch.Tensor, x2: torch.Tensor, elem: torch.Tensor
) -> torch.Tensor:
    """Per-feature reconstruction error, ``(B, F)``.

    Same 0.5/0.5 blend as :func:`anomaly_scores`, without the feature-dimension
    mean. Column ``j`` corresponds to ``feature_names[j]``.
    """
    err = 0.5 * (x1 - elem) ** 2 + 0.5 * (x2 - elem) ** 2
    return err.squeeze(0)


@torch.no_grad()
def score_windows(
    model: torch.nn.Module,
    windows: np.ndarray,
    batch_size: int = 256,
    device: str | torch.device = "cpu",
) -> tuple[np.ndarray, np.ndarray]:
    """Score a batch of preprocessed windows.

    Parameters
    ----------
    windows:
        ``(n, 30, 45)`` float32, already through :class:`TranADPreprocessor`.
    batch_size:
        Matches the research default of 256. Batching affects only throughput,
        not semantics -- there is no cross-window state.

        It is not, however, bit-neutral: a different batch dimension selects a
        different BLAS GEMM blocking, which changes float32 accumulation order.
        Measured drift is ~1e-06 relative. Since the anomaly decision is a strict
        ``score > threshold`` comparison, a score sitting within that relative
        distance of the threshold could in principle flip with batch
        composition. For the verified SWaT windows the margins are 5.7x (normal,
        below) and 23.1x (attack, above), so this is not a practical concern
        here -- but a future alerting layer should not treat borderline scores as
        exactly reproducible across differing batch shapes.

    Returns
    -------
    ``(scores, feature_errors)`` with shapes ``(n,)`` and ``(n, 45)``.
    """
    arr = np.asarray(windows, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    if arr.ndim != 3:
        raise ValueError(
            f"expected (batch, window, n_features), got shape {arr.shape}"
        )
    if arr.shape[0] == 0:
        raise ValueError("empty batch")

    model.eval()
    out_scores: list[np.ndarray] = []
    out_errors: list[np.ndarray] = []
    for i in range(0, len(arr), batch_size):
        chunk = torch.as_tensor(arr[i : i + batch_size], dtype=torch.float32, device=device)
        x1, x2, elem = reconstruct(model, chunk)
        out_scores.append(anomaly_scores(x1, x2, elem).cpu().numpy())
        out_errors.append(per_feature_errors(x1, x2, elem).cpu().numpy())

    return np.concatenate(out_scores), np.concatenate(out_errors)
