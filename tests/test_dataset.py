"""Dataset loader tests.

Split in two on purpose:

* Synthetic-CSV tests build tiny fixtures in ``tmp_path`` and run everywhere.
  They cover the parsing contract -- header whitespace, empty-cell rejection,
  timestamp merge ordering, subsampling, constant-feature detection.
* ``@pytest.mark.dataset`` tests assert the real dataset's documented shape and
  are skipped when the licensed CSVs are absent.

The dataset-gated block is where **provenance open item 4** is discharged: it
asserts the five features the research pipeline silently froze do vary in the
clean reconstruction, so the gap really is gone rather than assumed gone.
"""

from __future__ import annotations

import numpy as np
import pytest

from ml.src.dataset import (
    ATTACK_V0_ROWS,
    DatasetError,
    attack_segments,
    load_attack_v0,
    load_research_concat,
    sliding_windows,
)

# The six features emptied by the header-spelling mismatch (provenance 5.2).
# P204 is excluded from the "must vary" assertion: 5.3 leaves open whether its
# zero variance is a gap artifact or physically real, and the loader is what
# finally decides. P206 is separately and genuinely constant.
GAP_VICTIMS_EXPECTED_TO_VARY = ("MV101", "AIT201", "MV201", "P201", "MV303")

FEATS = ("FIT101", "LIT101", "MV101")
HEADER = "Timestamp, FIT101,LIT101, MV101,Normal/Attack\n"


def _write_csv(path, rows, header: str = HEADER) -> None:
    """rows: list of (timestamp, fit101, lit101, mv101, label) as strings."""
    path.write_text(header + "".join(",".join(r) + "\n" for r in rows))


def _row(ts: str, a: str = "1.0", b: str = "2.0", c: str = "3.0", label: str = "Normal"):
    return (ts, a, b, c, label)


# --------------------------------------------------------------- parsing contract

def test_header_whitespace_is_stripped(tmp_path):
    """Seven real SWaT columns carry a leading space; names must still match."""
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    assert s.feature_names == FEATS      # " MV101" resolved to "MV101"
    assert len(s) == 2


def test_empty_cell_is_rejected_not_imputed(tmp_path):
    """The clean reconstruction is gap-free; imputing is the calibration bug."""
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", c="")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    with pytest.raises(DatasetError, match="empty value for 'MV101'"):
        load_attack_v0(FEATS, tmp_path, verify_rows=False)


def test_error_names_the_offending_file_and_line(tmp_path):
    _write_csv(
        tmp_path / "attack.csv",
        [_row("28/12/2015 10:00:00 AM"), _row("28/12/2015 10:00:01 AM", b="")],
    )
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:02 AM")])
    with pytest.raises(DatasetError, match=r"attack\.csv line 3"):
        load_attack_v0(FEATS, tmp_path, verify_rows=False)


def test_labels_parse_attack_variants(tmp_path):
    """Lowercase + space-strip substring test, absorbing the 'A ttack' typo."""
    _write_csv(
        tmp_path / "attack.csv",
        [
            _row("28/12/2015 10:00:00 AM", label="Attack"),
            _row("28/12/2015 10:00:01 AM", label="A ttack"),
            _row("28/12/2015 10:00:02 AM", label="attack"),
        ],
    )
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:03 AM", label="Normal")])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    np.testing.assert_array_equal(s.labels, [1, 1, 1, 0])


def test_timestamp_sort_restores_interleaved_order(tmp_path):
    """attack.csv is label-filtered; the merge must rebuild the real sequence."""
    _write_csv(
        tmp_path / "attack.csv",
        [_row("28/12/2015 10:00:01 AM", a="10", label="Attack")],
    )
    _write_csv(
        tmp_path / "normal.csv",
        [
            _row("28/12/2015 10:00:00 AM", a="20"),
            _row("28/12/2015 10:00:02 AM", a="30"),
        ],
    )
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    np.testing.assert_array_equal(s.values[:, 0], [20, 10, 30])
    np.testing.assert_array_equal(s.labels, [0, 1, 0])
    assert (np.diff(s.timestamps).astype(int) > 0).all()


def test_twelve_hour_clock_is_parsed(tmp_path):
    """PM must sort after AM -- a 24h misparse would silently reorder the day."""
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 02:00:00 PM", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 02:00:00 AM")])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    np.testing.assert_array_equal(s.labels, [0, 1])          # AM first
    assert (s.timestamps[1] - s.timestamps[0]).astype(int) == 12 * 3600


def test_normal_block1_row_limit_is_applied(tmp_path, monkeypatch):
    """Only the gap-free prefix of normal.csv is read."""
    import ml.src.dataset as ds

    monkeypatch.setattr(ds, "NORMAL_BLOCK1_ROWS", 2)
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", label="Attack")])
    _write_csv(
        tmp_path / "normal.csv",
        [_row(f"28/12/2015 10:00:0{i} AM") for i in range(1, 5)],
    )
    s = ds.load_attack_v0(FEATS, tmp_path, verify_rows=False)
    assert len(s) == 3           # 1 attack + 2 normal, rows 3-4 excluded


def test_row_count_verification_raises(tmp_path):
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    with pytest.raises(DatasetError, match=f"expected {ATTACK_V0_ROWS:,} rows"):
        load_attack_v0(FEATS, tmp_path)


def test_missing_file_names_the_env_var(tmp_path):
    with pytest.raises(DatasetError, match="SWAT_DATASET_DIR"):
        load_attack_v0(FEATS, tmp_path, verify_rows=False)


def test_wrong_field_count_raises(tmp_path):
    (tmp_path / "attack.csv").write_text(HEADER + "28/12/2015 10:00:00 AM,1.0,2.0\n")
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    with pytest.raises(DatasetError, match="expected 5 fields, got 3"):
        load_attack_v0(FEATS, tmp_path, verify_rows=False)


def test_unknown_feature_name_raises(tmp_path):
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    with pytest.raises(DatasetError, match="missing 1 expected feature"):
        load_attack_v0(("FIT101", "NOPE"), tmp_path, verify_rows=False)


# ------------------------------------------------------------ constant features

def test_constant_features_are_measured(tmp_path):
    """LIT101 never changes here; MV101 does. Detected, not assumed."""
    _write_csv(
        tmp_path / "attack.csv",
        [
            _row("28/12/2015 10:00:00 AM", a="1", b="5", c="1", label="Attack"),
            _row("28/12/2015 10:00:01 AM", a="2", b="5", c="9", label="Attack"),
        ],
    )
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:02 AM", a="3", b="5", c="4")])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    assert s.constant_features == ("LIT101",)


def test_subsample_recomputes_constant_features(tmp_path):
    """A feature can be constant in a subsample while varying in the full series."""
    rows = [
        _row(f"28/12/2015 10:00:0{i} AM", a="1", b=str(i % 2), c="1")
        for i in range(4)
    ]
    _write_csv(tmp_path / "attack.csv", rows[:1])
    _write_csv(tmp_path / "normal.csv", rows[1:])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    assert "LIT101" not in s.constant_features        # alternates 0,1,0,1
    assert "LIT101" in s.subsample(2).constant_features  # rows 0,2 -> both 0


def test_subsample_step_one_is_identity(tmp_path):
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    assert s.subsample(1) is s


def test_subsample_rejects_zero(tmp_path):
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM")])
    s = load_attack_v0(FEATS, tmp_path, verify_rows=False)
    with pytest.raises(DatasetError, match="must be >= 1"):
        s.subsample(0)


def test_attack_ratio(tmp_path):
    _write_csv(
        tmp_path / "attack.csv",
        [_row(f"28/12/2015 10:00:0{i} AM", label="Attack") for i in range(3)],
    )
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:05 AM")])
    assert load_attack_v0(FEATS, tmp_path, verify_rows=False).attack_ratio == 0.75


# ------------------------------------------------------- research concat + ffill

def test_research_concat_forward_fills_empties(tmp_path):
    """Reproduces the defect on purpose: ffill carries the last value forward."""
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", c="7", label="Attack")])
    _write_csv(
        tmp_path / "normal.csv",
        [_row("28/12/2015 10:00:01 AM", c=""), _row("28/12/2015 10:00:02 AM", c="")],
    )
    s = load_research_concat(FEATS, tmp_path)
    # MV101 frozen at the attack file's last value -- exactly the 5.2 mechanism
    np.testing.assert_array_equal(s.values[:, 2], [7.0, 7.0, 7.0])
    assert "MV101" in s.constant_features


def test_research_concat_leading_nan_becomes_zero(tmp_path):
    """fillna(0.0): no prior observation to carry."""
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 10:00:00 AM", c="", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:01 AM", c="4")])
    s = load_research_concat(FEATS, tmp_path)
    np.testing.assert_array_equal(s.values[:, 2], [0.0, 4.0])


def test_research_concat_does_not_sort_by_time(tmp_path):
    """The notebook concatenated in glob order, not chronological order."""
    _write_csv(tmp_path / "attack.csv", [_row("28/12/2015 11:00:00 AM", a="9", label="Attack")])
    _write_csv(tmp_path / "normal.csv", [_row("28/12/2015 10:00:00 AM", a="1")])
    s = load_research_concat(FEATS, tmp_path)
    np.testing.assert_array_equal(s.values[:, 0], [9, 1])   # attack.csv first
    np.testing.assert_array_equal(s.labels, [1, 0])


# --------------------------------------------------------------- attack_segments

def test_attack_segments_finds_half_open_runs():
    labels = np.array([0, 1, 1, 0, 0, 1, 0])
    assert attack_segments(labels) == [(1, 3), (5, 6)]


def test_attack_segments_at_boundaries():
    assert attack_segments(np.array([1, 1, 0])) == [(0, 2)]
    assert attack_segments(np.array([0, 1, 1])) == [(1, 3)]
    assert attack_segments(np.array([1])) == [(0, 1)]


def test_attack_segments_empty_and_all_normal():
    assert attack_segments(np.array([], dtype=int)) == []
    assert attack_segments(np.zeros(5, dtype=int)) == []


def test_attack_segments_merges_adjacent():
    """Contiguous 1s are one run -- why 36 launched attacks give 35 label segments."""
    assert attack_segments(np.ones(10, dtype=int)) == [(0, 10)]


def test_attack_segments_rejects_2d():
    with pytest.raises(DatasetError, match="1-D"):
        attack_segments(np.zeros((3, 3)))


# --------------------------------------------------------------- sliding_windows

def test_sliding_windows_shape_and_content():
    values = np.arange(20).reshape(10, 2)
    w = sliding_windows(values, window=3)
    assert w.shape == (8, 3, 2)
    np.testing.assert_array_equal(w[0], values[0:3])
    np.testing.assert_array_equal(w[-1], values[7:10])


def test_sliding_windows_is_a_view_not_a_copy():
    """486 MB saved on the real dataset; the result must stay read-only."""
    values = np.arange(20.0).reshape(10, 2)
    assert sliding_windows(values, 3).base is not None


def test_sliding_windows_exact_length_gives_one_window():
    assert sliding_windows(np.zeros((3, 2)), 3).shape == (1, 3, 2)


def test_sliding_windows_too_short_raises():
    with pytest.raises(DatasetError, match="at least 5 samples"):
        sliding_windows(np.zeros((4, 2)), 5)


def test_sliding_windows_rejects_wrong_rank():
    with pytest.raises(DatasetError, match="2-D"):
        sliding_windows(np.zeros((4, 2, 2)), 2)


# ============================================================ real dataset tests

@pytest.mark.dataset
class TestRealDataset:
    """Asserts the documented shape of the licensed SWaT CSVs."""

    @pytest.fixture(scope="class")
    def clean(self, swat_dataset_dir, feature_names):
        return load_attack_v0(feature_names, swat_dataset_dir)

    def test_row_count_matches_provenance(self, clean):
        assert len(clean) == ATTACK_V0_ROWS

    def test_attack_ratio_is_the_true_rate(self, clean):
        """12.1402%, versus the 3.79% the duplicated normal block deflated it to."""
        assert clean.attack_ratio == pytest.approx(0.121402, abs=1e-6)

    def test_no_missing_values(self, clean):
        assert np.isfinite(clean.values).all()

    def test_timestamps_are_non_decreasing(self, clean):
        assert (np.diff(clean.timestamps).astype(np.int64) >= 0).all()

    def test_thirty_five_attack_segments(self, clean):
        """Cite 35 for this dataset's labels; 36 refers to attacks launched."""
        assert len(attack_segments(clean.labels)) == 35

    def test_gap_victim_features_are_not_frozen(self, clean):
        """Provenance open item 4: prove the 5.2 defect is absent here.

        These five features were single-valued across 100% of the split the
        shipped threshold was calibrated on. If any is constant in the clean
        reconstruction, the reconstruction is wrong.
        """
        frozen = set(clean.constant_features)
        for name in GAP_VICTIMS_EXPECTED_TO_VARY:
            assert name not in frozen, (
                f"{name} is constant in the clean Attack_v0 reconstruction; "
                f"the 5.2 data gap has not actually been removed"
            )

    def test_constant_features_are_reported(self, clean):
        """The clean slice's only single-valued feature is P301 -- verified, not a surprise.

        This is a different question from "what did the scaler freeze", and the
        two must not be conflated (an earlier version of this test asserted the
        constants were a subset of {P204, P206} and was wrong for exactly that
        reason). The scaler was fit on the normal-only training split of the
        *research concat*, which includes the defective block-2 duplicate; there
        P204 and P206 have var_ == 0 (provenance 5.2-5.3). ``constant_features``
        is instead measured over the clean Attack_v0 slice (attack.csv + normal
        block 1, attack rows included), a different distribution:

        * P204, P206 -- constant only in *normal* operation; both are actuated to
          2.0 during attacks, so across this attack-bearing slice they vary. Their
          training-time zero variance was the block-2 gap (P204) and
          normal-operation constancy (P206), not a property of this slice.
        * P301 -- 1.0 across every row of both source files in this date range
          (independently verified in attack.csv and normal.csv rows 1..395,298).
          It does reach 2.0 elsewhere in the full normal record, so the scaler did
          not freeze it (var_ != 0) -- just not in the period this slice covers.

        See provenance section 5.6.
        """
        assert set(clean.constant_features) == {"P301"}

    def test_subsampled_window_count(self, clean):
        """89,984 samples at ::5 -> 89,955 stride-1 windows of length 30."""
        sub = clean.subsample(5)
        assert len(sub) == 89_984
        assert len(sliding_windows(sub.values, 30)) == 89_955
