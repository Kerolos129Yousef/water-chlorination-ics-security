"""Shared fixtures for the TranAD inference tests.

Adds the repository root to ``sys.path`` so ``ml.src`` imports without an
install step. ``ml/`` has no ``__init__.py`` and works as a PEP 420 implicit
namespace package; ``ml/src/`` is a regular package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ARTIFACTS_DIR = REPO_ROOT / "ml" / "artifacts" / "swat_TranAD"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

WINDOW = 30
N_FEATURES = 45
FLAT_DIM = WINDOW * N_FEATURES  # 1350


def pytest_configure(config: pytest.Config) -> None:
    # torch warns that the encoder fast path is unavailable because the research
    # architecture uses batch_first=False. Expected and required for parity --
    # changing it would alter numerics.
    config.addinivalue_line(
        "filterwarnings", "ignore:enable_nested_tensor is True:UserWarning"
    )
    # Tests that need the licensed SWaT CSVs, which are not in version control.
    # Everything else must pass without them; that property is load-bearing.
    config.addinivalue_line(
        "markers", "dataset: requires the local SWaT dataset (skipped if absent)"
    )


@pytest.fixture(scope="session")
def swat_dataset_dir() -> Path:
    """The local SWaT CSV directory, or skip the test.

    Honours ``$SWAT_DATASET_DIR``. The dataset is licensed from iTrust, SUTD and
    excluded from version control, so these tests are opportunistic.
    """
    from ml.src.dataset import default_dataset_dir

    d = default_dataset_dir()
    if not (d / "attack.csv").is_file() or not (d / "normal.csv").is_file():
        pytest.skip(
            f"SWaT dataset not found at {d}; set SWAT_DATASET_DIR to enable "
            f"dataset-gated tests"
        )
    return d


@pytest.fixture(scope="session")
def artifacts_dir() -> Path:
    assert ARTIFACTS_DIR.is_dir(), f"missing artifacts dir: {ARTIFACTS_DIR}"
    return ARTIFACTS_DIR


@pytest.fixture(scope="session")
def feature_names(artifacts_dir: Path) -> list[str]:
    return json.loads((artifacts_dir / "feature_names.json").read_text())


@pytest.fixture(scope="session")
def threshold_json(artifacts_dir: Path) -> dict:
    return json.loads((artifacts_dir / "threshold.json").read_text())


@pytest.fixture(scope="session")
def standard_scaler(artifacts_dir: Path):
    return joblib.load(artifacts_dir / "scaler.joblib")


@pytest.fixture(scope="session")
def minmax_scaler(artifacts_dir: Path):
    return joblib.load(artifacts_dir / "minmax_scaler.joblib")


@pytest.fixture(scope="session")
def golden() -> dict:
    path = FIXTURES_DIR / "golden_vectors.json"
    assert path.is_file(), (
        f"missing {path}; regenerate with "
        "tests/fixtures/generate_golden_vectors.py (requires the SWaT dataset)"
    )
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def golden_cases(golden: dict) -> dict:
    return {case["name"]: case for case in golden["cases"]}


@pytest.fixture(scope="session")
def preprocessor(artifacts_dir: Path):
    from ml.src.preprocessing import TranADPreprocessor

    return TranADPreprocessor(artifacts_dir)


@pytest.fixture(scope="session")
def detector(artifacts_dir: Path):
    from ml.src.detector import TranADDetector

    return TranADDetector.from_artifacts(artifacts_dir)


@pytest.fixture
def normal_window(golden_cases: dict) -> np.ndarray:
    """The real gap-free normal window from Attack_v0 (scores below threshold)."""
    return np.array(golden_cases["normal"]["raw_window"], dtype=np.float64)


@pytest.fixture
def attack_window(golden_cases: dict) -> np.ndarray:
    """The real attack window from Attack_v0 (scores above threshold)."""
    return np.array(golden_cases["attack"]["raw_window"], dtype=np.float64)
