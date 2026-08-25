"""Detector facade tests: interface, threshold decision, isolation."""

from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from ml.src.detector import THRESHOLD_CAVEAT, DetectionResult, TranADDetector
from tests.conftest import N_FEATURES, WINDOW


def test_loads_threshold_from_artifact(detector, threshold_json):
    assert detector.threshold == threshold_json["threshold"] == 9.269035945180804e-05
    assert detector.threshold_metadata["percentile"] == 99
    assert detector.threshold_metadata["metric"] == "tranad_score"
    assert detector.threshold_metadata["window"] == WINDOW


def test_exposes_contract(detector, feature_names):
    assert detector.window == WINDOW
    assert detector.n_features == N_FEATURES
    assert list(detector.feature_names) == feature_names


def test_result_has_all_required_fields(detector, normal_window):
    r = detector.score(normal_window)
    assert isinstance(r, DetectionResult)
    assert isinstance(r.anomaly_score, float)
    assert isinstance(r.threshold, float)
    assert isinstance(r.is_anomaly, bool)
    assert isinstance(r.feature_errors, np.ndarray)
    assert r.feature_errors.shape == (N_FEATURES,)


def test_result_is_immutable(detector, normal_window):
    r = detector.score(normal_window)
    with pytest.raises(Exception):
        r.anomaly_score = 0.0  # frozen dataclass


def test_threshold_decision_normal_below(detector, normal_window):
    """Real gap-free normal window from Attack_v0 must not alarm."""
    r = detector.score(normal_window)
    assert r.anomaly_score <= r.threshold
    assert r.is_anomaly is False


def test_threshold_decision_attack_above(detector, attack_window):
    """Real attack window must alarm."""
    r = detector.score(attack_window)
    assert r.anomaly_score > r.threshold
    assert r.is_anomaly is True


def test_threshold_decision_is_strictly_greater(detector, normal_window, monkeypatch):
    """Research code used `scores > thr`; a score exactly at the threshold is normal."""
    r = detector.score(normal_window)
    monkeypatch.setattr(detector, "_threshold", r.anomaly_score)
    assert detector.score(normal_window).is_anomaly is False


def test_top_features_ranks_by_error(detector, attack_window):
    r = detector.score(attack_window)
    top = r.top_features(5)
    assert len(top) == 5
    assert [name for name, _ in top] == [
        detector.feature_names[i] for i in np.argsort(r.feature_errors)[::-1][:5]
    ]
    assert all(top[i][1] >= top[i + 1][1] for i in range(len(top) - 1))


def test_score_batch_matches_individual_scores(detector, normal_window, attack_window):
    """Batching must not change the decision.

    Compared at rtol=1e-5 rather than exactly: a different batch dimension
    selects a different BLAS blocking, shifting float32 accumulation by ~1e-06
    relative (measured). See
    test_scoring.test_batching_does_not_change_results_beyond_float32_noise.
    """
    batch = detector.score_batch(np.stack([normal_window, attack_window]))
    assert len(batch) == 2
    assert batch[0].anomaly_score == pytest.approx(
        detector.score(normal_window).anomaly_score, rel=1e-5
    )
    assert batch[1].anomaly_score == pytest.approx(
        detector.score(attack_window).anomaly_score, rel=1e-5
    )
    # The decision itself must be unaffected.
    assert batch[0].is_anomaly is False
    assert batch[1].is_anomaly is True


def test_score_records_matches_array_path(detector, normal_window, feature_names):
    records = [
        {name: float(normal_window[t, j]) for j, name in enumerate(feature_names)}
        for t in range(WINDOW)
    ]
    assert detector.score_records(records).anomaly_score == pytest.approx(
        detector.score(normal_window).anomaly_score, rel=0, abs=0
    )


def test_repeated_scoring_is_deterministic(detector, attack_window):
    first = detector.score(attack_window)
    for _ in range(3):
        again = detector.score(attack_window)
        assert again.anomaly_score == first.anomaly_score
        np.testing.assert_array_equal(again.feature_errors, first.feature_errors)


def test_threshold_caveat_is_exposed(detector):
    """The calibration caveat must be reachable, not buried in a doc."""
    assert detector.threshold_caveat == THRESHOLD_CAVEAT
    for name in ("MV101", "AIT201", "MV201", "P201", "P204", "MV303"):
        assert name in detector.threshold_caveat
    assert "0.7934" in detector.threshold_caveat


def test_window_mismatch_against_threshold_json_raises(tmp_path, artifacts_dir):
    """A threshold calibrated for a different window must be refused."""
    staged = tmp_path / "art"
    shutil.copytree(artifacts_dir, staged)
    meta = json.loads((staged / "threshold.json").read_text())
    meta["window"] = 60
    (staged / "threshold.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="calibrated for window=60"):
        TranADDetector.from_artifacts(staged)


def test_missing_artifact_raises(tmp_path, artifacts_dir):
    staged = tmp_path / "art"
    shutil.copytree(artifacts_dir, staged)
    (staged / "threshold.json").unlink()
    with pytest.raises(FileNotFoundError, match="threshold.json"):
        TranADDetector.from_artifacts(staged)


def test_detector_module_imports_no_service_frameworks():
    """Phase 1 is a pure library: no FastAPI, DB, CLI or UI coupling."""
    import ml.src.detector as mod
    import ml.src.model
    import ml.src.preprocessing
    import ml.src.scoring

    forbidden = {
        "fastapi", "starlette", "uvicorn", "pydantic", "flask",
        "sqlalchemy", "sqlite3", "psycopg2", "redis",
        "argparse", "click", "typer", "streamlit", "dash",
    }
    for module in (mod, ml.src.model, ml.src.preprocessing, ml.src.scoring):
        src = open(module.__file__).read()
        for name in forbidden:
            assert f"import {name}" not in src, f"{module.__name__} imports {name}"


def test_artifacts_are_not_modified_by_scoring(detector, artifacts_dir, attack_window):
    """Inference must never write to the artifact directory."""
    import hashlib

    def digests():
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(artifacts_dir.iterdir())
            if p.is_file()
        }

    before = digests()
    detector.score(attack_window)
    detector.score_batch(np.stack([attack_window, attack_window]))
    assert digests() == before
