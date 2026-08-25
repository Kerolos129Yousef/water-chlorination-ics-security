"""Model loading and architecture-drift tests.

The architecture is a hard contract with ``model.pt``: it stores weights only, so
every ``state_dict`` key and tensor shape must match what ``TranAD(45, 30)``
builds. A drifting architecture is caught here rather than surfacing as subtly
wrong scores.
"""

from __future__ import annotations

import torch

from ml.src.model import TranAD, load_tranad
from tests.conftest import N_FEATURES, WINDOW

# Tensor shapes that encode the architecture. Derived from model.pt and
# independently reproduced by TranAD(feats=45, window=30). Any change to feats,
# window, nhead, dim_feedforward or the layer counts alters at least one of these.
CRITICAL_SHAPES = {
    "pos_encoder.pe": (WINDOW, 1, 2 * N_FEATURES),                       # window, d_model
    "fcn.0.weight": (N_FEATURES, 2 * N_FEATURES),                        # feats, d_model
    "fcn.0.bias": (N_FEATURES,),
    "transformer_encoder.layers.0.linear1.weight": (16, 2 * N_FEATURES),  # dim_feedforward
    "transformer_encoder.layers.0.self_attn.in_proj_weight": (3 * 2 * N_FEATURES, 2 * N_FEATURES),
    "transformer_decoder1.layers.0.multihead_attn.in_proj_weight": (3 * 2 * N_FEATURES, 2 * N_FEATURES),
    "transformer_decoder2.layers.0.multihead_attn.in_proj_weight": (3 * 2 * N_FEATURES, 2 * N_FEATURES),
}

EXPECTED_TENSOR_COUNT = 51
EXPECTED_PARAM_ELEMENTS = 180_993


def test_state_dict_loads_strict(artifacts_dir):
    """strict=True must succeed: a partial load leaves layers randomly initialised."""
    model = load_tranad(artifacts_dir / "model.pt", feats=N_FEATURES, window=WINDOW)
    assert isinstance(model, TranAD)


def test_incomplete_state_dict_is_rejected(artifacts_dir, tmp_path):
    """A checkpoint missing a tensor must raise, not load partially.

    This is what actually enforces strict=True. Without it, a weakened loader
    would silently leave `fcn.0.weight` randomly initialised and still return
    plausible-looking scores. Writes only to tmp_path; model.pt is untouched.
    """
    import pytest

    saved = torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    truncated = {k: v for k, v in saved.items() if k != "fcn.0.weight"}
    path = tmp_path / "truncated.pt"
    torch.save(truncated, path)

    with pytest.raises(RuntimeError, match="[Mm]issing key"):
        load_tranad(path, feats=N_FEATURES, window=WINDOW)


def test_unexpected_state_dict_key_is_rejected(artifacts_dir, tmp_path):
    """An extra tensor must raise too: it signals a checkpoint/code mismatch."""
    import pytest

    saved = torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    extended = dict(saved)
    extended["fcn.99.weight"] = torch.zeros(1)
    path = tmp_path / "extended.pt"
    torch.save(extended, path)

    with pytest.raises(RuntimeError, match="[Uu]nexpected key"):
        load_tranad(path, feats=N_FEATURES, window=WINDOW)


def test_model_is_in_eval_mode(artifacts_dir):
    """dropout=0.1 at three sites makes train() mode non-deterministic."""
    model = load_tranad(artifacts_dir / "model.pt", feats=N_FEATURES, window=WINDOW)
    assert model.training is False
    for module in model.modules():
        if isinstance(module, torch.nn.Dropout):
            assert module.training is False


def test_architecture_matches_checkpoint_exactly(artifacts_dir):
    """Keys AND shapes must match; catches any architecture drift."""
    saved = torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    built = TranAD(feats=N_FEATURES, window=WINDOW).state_dict()

    assert set(built) == set(saved), (
        f"state_dict key mismatch. only-in-code={sorted(set(built) - set(saved))} "
        f"only-in-checkpoint={sorted(set(saved) - set(built))}"
    )
    mismatched = {
        k: (tuple(built[k].shape), tuple(saved[k].shape))
        for k in saved
        if tuple(built[k].shape) != tuple(saved[k].shape)
    }
    assert not mismatched, f"tensor shape mismatch (built, saved): {mismatched}"


def test_checkpoint_critical_shapes(artifacts_dir):
    """Pin the shapes that encode feats/window/nhead/dim_feedforward."""
    saved = torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    for key, shape in CRITICAL_SHAPES.items():
        assert key in saved, f"missing {key} in checkpoint"
        assert tuple(saved[key].shape) == shape, f"{key}: {tuple(saved[key].shape)} != {shape}"


def test_checkpoint_tensor_and_parameter_counts(artifacts_dir):
    saved = torch.load(artifacts_dir / "model.pt", map_location="cpu", weights_only=True)
    assert len(saved) == EXPECTED_TENSOR_COUNT
    assert sum(t.numel() for t in saved.values()) == EXPECTED_PARAM_ELEMENTS


def test_wrong_feature_count_fails_to_load(artifacts_dir):
    """A mismatched feats must raise, not silently reshape."""
    with __import__("pytest").raises(RuntimeError):
        load_tranad(artifacts_dir / "model.pt", feats=44, window=WINDOW)


def test_wrong_window_fails_to_load(artifacts_dir):
    """pos_encoder.pe is window-sized, so a wrong window is a load-time error."""
    with __import__("pytest").raises(RuntimeError):
        load_tranad(artifacts_dir / "model.pt", feats=N_FEATURES, window=29)


def test_missing_weights_file_raises(tmp_path):
    with __import__("pytest").raises(FileNotFoundError):
        load_tranad(tmp_path / "nope.pt", feats=N_FEATURES, window=WINDOW)


def test_forward_returns_two_reconstructions(artifacts_dir):
    """TranAD is a two-decoder model; both outputs are part of the contract."""
    model = load_tranad(artifacts_dir / "model.pt", feats=N_FEATURES, window=WINDOW)
    src = torch.zeros(WINDOW, 2, N_FEATURES)
    elem = src[-1:, :, :]
    with torch.no_grad():
        x1, x2 = model(src, elem)
    assert x1.shape == (1, 2, N_FEATURES)
    assert x2.shape == (1, 2, N_FEATURES)
    # Sigmoid-terminated fcn, so both reconstructions are bounded to (0, 1).
    for out in (x1, x2):
        assert float(out.min()) >= 0.0
        assert float(out.max()) <= 1.0
