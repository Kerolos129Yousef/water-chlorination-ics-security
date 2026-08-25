"""TranAD architecture and artifact loader.

The class and attribute names in this module are part of the on-disk contract:
``ml/artifacts/swat_TranAD/model.pt`` is a ``state_dict``, so every submodule
attribute name below is a key in that file. Renaming ``pos_encoder``,
``transformer_encoder``, ``transformer_decoder1``, ``transformer_decoder2`` or
``fcn`` breaks ``strict=True`` loading.

Reconstructed verbatim from ``research/notebooks/swat/SWaT_Anomaly_Detection.ipynb``
(cell 15). See ``docs/provenance/tranad_swat_provenance.md`` for the audit trail.
"""

from __future__ import annotations

import math
from pathlib import Path

import torch
import torch.nn as nn

__all__ = ["PositionalEncoding", "TranAD", "load_tranad"]


class PositionalEncoding(nn.Module):
    """Sinusoidal positional encoding over a sequence-first tensor.

    Registers ``pe`` with shape ``(max_len, 1, d_model)`` so it broadcasts over
    the batch dimension of a ``(W, B, d_model)`` input.
    """

    def __init__(self, d_model: int, max_len: int, dropout: float = 0.1) -> None:
        super().__init__()
        self.dropout = nn.Dropout(dropout)
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div = torch.exp(
            torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.dropout(x + self.pe[: x.size(0)])


class TranAD(nn.Module):
    """Two-decoder adversarial transformer autoencoder (Tuli et al., 2022).

    Reconstructs the *last* timestep of each window twice: ``x1`` unconditioned,
    then ``x2`` conditioned on the phase-1 squared error as a focus score. The
    output FCN ends in Sigmoid, which is why inputs must be MinMax-scaled to
    [0, 1] rather than standard-scaled only.
    """

    def __init__(self, feats: int, window: int) -> None:
        super().__init__()
        self.n_feats = feats
        self.pos_encoder = PositionalEncoding(2 * feats, window)
        enc = nn.TransformerEncoderLayer(
            d_model=2 * feats, nhead=feats, dim_feedforward=16, dropout=0.1
        )
        self.transformer_encoder = nn.TransformerEncoder(enc, 1)
        dec1 = nn.TransformerDecoderLayer(
            d_model=2 * feats, nhead=feats, dim_feedforward=16, dropout=0.1
        )
        self.transformer_decoder1 = nn.TransformerDecoder(dec1, 1)
        dec2 = nn.TransformerDecoderLayer(
            d_model=2 * feats, nhead=feats, dim_feedforward=16, dropout=0.1
        )
        self.transformer_decoder2 = nn.TransformerDecoder(dec2, 1)
        self.fcn = nn.Sequential(nn.Linear(2 * feats, feats), nn.Sigmoid())

    def encode(self, src: torch.Tensor, c: torch.Tensor, tgt: torch.Tensor):
        src = torch.cat((src, c), dim=2) * math.sqrt(self.n_feats)
        src = self.pos_encoder(src)
        memory = self.transformer_encoder(src)
        return tgt.repeat(1, 1, 2), memory

    def forward(self, src: torch.Tensor, tgt: torch.Tensor):
        """``src``: (W, B, F) scaled window. ``tgt``: (1, B, F) last timestep."""
        c = torch.zeros_like(src)
        x1 = self.fcn(self.transformer_decoder1(*self.encode(src, c, tgt)))
        c = (x1 - src) ** 2
        x2 = self.fcn(self.transformer_decoder2(*self.encode(src, c, tgt)))
        return x1, x2


def load_tranad(
    weights_path: str | Path,
    feats: int,
    window: int,
    device: str | torch.device = "cpu",
) -> TranAD:
    """Build a TranAD and load ``weights_path`` into it.

    Always ``strict=True``: a silently-partial load would produce randomly
    initialised layers that still yield plausible-looking scores. Always
    ``weights_only=True``: the checkpoint is data, never code.

    The returned model is in ``eval()`` mode, which disables the three
    ``dropout=0.1`` sites. Scoring a model left in ``train()`` mode is
    non-deterministic.
    """
    weights_path = Path(weights_path)
    if not weights_path.is_file():
        raise FileNotFoundError(f"TranAD weights not found: {weights_path}")

    model = TranAD(feats=feats, window=window)
    state_dict = torch.load(weights_path, map_location="cpu", weights_only=True)
    model.load_state_dict(state_dict, strict=True)
    model.to(device)
    model.eval()
    return model
