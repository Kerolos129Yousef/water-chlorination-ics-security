# TranAD / SWaT — Artifact Provenance & Reproducibility Record

**Verified:** 2026-08-21
**Status:** FULL PARITY — the artifacts in `ml/artifacts/swat_TranAD/` are exactly
reproducible from the local SWaT dataset using the research notebook's default `CONFIG`.
**Machine-readable form:** [`ml/configs/tranad_swat.yaml`](../../ml/configs/tranad_swat.yaml)

This document exists because the script that exported the five artifacts was lost, and the
research notebooks were committed with **zero saved outputs** (`execution_count: null` on
every cell). Without this record, the preprocessing contract was recoverable only by
reverse-engineering artifact bytes. It is now written down.

---

## 1. Scope and constraints

The artifacts are **immutable**. This verification was read-only:

- No file under `ml/artifacts/swat_TranAD/` was modified.
- No dataset file was modified.
- TranAD was **not** retrained.
- The threshold was **not** recalibrated (see §5 — deliberately deferred).

---

## 2. Dataset

**Location:** `$HOME/Downloads/SWaT/SWaT-dataset/` — local only; **not** in version control
(`.gitignore` excludes `*.csv`, and the data is licensed from iTrust, SUTD).

| File | Data rows | Labels | SHA-256 (first 16) |
|---|---:|---|---|
| `attack.csv` | 54,621 | 100% `Attack` | `81977a6206ba9547` |
| `normal.csv` | 1,387,098 | 100% `Normal` | `efa3dfd271fd3340` |
| `merged.csv` | 1,441,719 | both | *(excluded — see below)* |

All three share an identical 53-column header (md5-matched): `Timestamp` + **51 process
columns** + `Normal/Attack`. Seven column names carry a leading space (` MV101`, ` AIT201`,
` MV201`, ` P201`, ` P202`, ` P204`, ` MV303`); the notebook's `.strip()` normalises them.
No non-numeric tokens; every row has 53 fields.

### Which files the pipeline reads

The notebook's `FILE_GLOB` is `"[!mM]*.csv"`, which **excludes `merged.csv`**. `load_dataset`
uses `sorted(glob(...))`, so the concatenation order is **`attack.csv` first, then
`normal.csv`** (alphabetical) — 1,441,719 rows total.

> The exclusion is correct but for the wrong stated reason. The notebook comment says it
> prevents double-loading a merged file. `merged.csv` also carries the **identical**
> 991,800-row data gap described in §5, so it is not a cleaner alternative.

---

## 3. Verified preprocessing chain

```
attack.csv + normal.csv            1,441,719 rows (51 process columns)
        ↓  drop near-constant columns (std <= 1e-8)
45 features, ordered               6 dropped: P202, P401, P404, P502, P601, P603
        ↓  X.iloc[::5]                          SUBSAMPLE = 5
288,344 rows                       277,419 Normal / 10,925 Attack
        ↓  normal-only, first 80% in time order  VAL_FRACTION = 0.2
221,936 training rows              ← StandardScaler fitted here
        ↓  StandardScaler.transform
        ↓  window(30, stride 1)
221,907 training windows
        ↓  reshape(n, -1)  C-order / timestep-major
(n, 1350)                          ← MinMaxScaler fitted here
        ↓  MinMaxScaler.transform → reshape(n, 30, 45)
TranAD input
```

### Parity results

Every value below was recomputed from the raw CSVs and compared against the artifact bytes.
Six independent constraints, all satisfied:

| Check | Recomputed | Artifact | Result |
|---|---|---|---|
| Training rows | 221,936 | `scaler.joblib` `n_samples_seen_` = 221,936 | **exact** |
| Training windows | 221,907 | `minmax_scaler.joblib` `n_samples_seen_` = 221,907 | **exact** |
| Feature selection | 45 kept / 6 dropped | `feature_names.json` | **exact — names *and* order** |
| `StandardScaler.mean_` | worst rel. err `1.98e-12` | 45 × float64 | **match** |
| `StandardScaler.var_` | worst rel. err `1.31e-11` | 45 × float64 | **match** |
| `MinMaxScaler.data_min_/data_max_` | worst abs. err `3.26e-06` | 1350 × float32 | **match** (float32 storage limit) |

`221,936 − 30 + 1 = 221,907` — the two `n_samples_seen_` values corroborate window=30 and
stride=1 independently of any other evidence.

### Pitfall: variance must be computed two-pass

A single-pass `Σx²/n − mean²` estimator reports a **false mismatch** on `AIT202`
(var 0.0151, mean 8.44) and `AIT501` (var 0.0031, mean 7.84) — relative errors of ~6e-9 from
catastrophic cancellation where `var ≪ mean²`. The stable two-pass form
(`Σ(x−mean)²/n`, sklearn's approach) reduces this to `3.5e-13`.

**Any production parity test must use the two-pass form** or it will report phantom failures
against correct artifacts.

---

## 4. Model contract

`model.pt` is a plain `OrderedDict` of 51 tensors (180,993 parameters, torch serialization
format v3, little-endian). It stores **weights only — no hyperparameters**. The constructor
arguments below were recovered from tensor shapes and are mandatory to rebuild the module
before `load_state_dict()`:

| Evidence | Implies |
|---|---|
| `pos_encoder.pe` = `(30, 1, 90)` | `window = 30`, `d_model = 90` |
| `fcn.0.weight` = `(45, 90)` | `feats = 45`, `d_model = 2 * feats` |
| `*.linear1.weight` = `(16, 90)` | `dim_feedforward = 16` |
| one `transformer_encoder.layers.0.*` group | 1 encoder layer |
| `transformer_decoder1` + `transformer_decoder2` | 2 decoders, 1 layer each, sharing one `fcn` |

→ `TranAD(feats=45, window=30)`, `nhead=45`, `dim_feedforward=16`, `dropout=0.1`.

Reference implementation: `research/notebooks/swat/SWaT_Anomaly_Detection.ipynb`, cell 15.
Copy the `TranAD` and `PositionalEncoding` classes **verbatim** — `state_dict` keys bind to
exact attribute names, so renaming any attribute breaks `load_state_dict(strict=True)`.

### Scoring

TranAD reconstructs **only the last timestep** of each window:

```python
src  = window.permute(1, 0, 2)   # (30, B, 45)
elem = src[-1:, :, :]            # (1, B, 45)
x1, x2 = model(src, elem)
score = 0.5 * ((x1 - elem) ** 2).mean(dim=2) + 0.5 * ((x2 - elem) ** 2).mean(dim=2)
```

Taking the squared error **before** the feature-dimension mean yields a 45-length
per-feature error vector — i.e. *which sensors drove this alert*. This is available at zero
extra cost with no model change, and is the direct evidence for the project's
"process-aware detection" claim. Production code should expose it.

### Environment pins

- **`scikit-learn == 1.7.2`** — embedded in both joblib files as `_sklearn_version`.
- **`numpy >= 2.0`** — the joblibs reference `numpy._core.multiarray`, a module that does not
  exist in numpy 1.x. Loading under numpy 1.x raises `ModuleNotFoundError`.
- **`torch == 2.9.1`** — the training version was **not recorded** (serialization format v3
  only implies `>= 1.6`), so this pin was *chosen*, not inherited. Rationale in
  `ml/requirements.txt`; the golden vectors were generated under it and lock it in place.

---

## 5. Threshold calibration caveat

> **This section is the reason this document exists. Read it before quoting any metric.**

### 5.1 What the threshold actually is

`threshold.json` = `9.269035945180804e-05`, recorded as `percentile: 99`,
`metric: "tranad_score"`, `window: 30`.

Traced to notebook cell 13, this is `thr_fixed = np.percentile(scores_val, 99)` — the 99th
percentile of TranAD scores on the **normal-validation** split. It is **not** the notebook's
best-F1 threshold, which was searched against the test set and is unusable in production.

The notebook metric that pairs with this threshold is **`F1_fixed`**, not the headline `F1`:

| Metric | Value | Note |
|---|---|---|
| **F1_fixed** (point-adjusted) | **0.7934** | ← the metric for the shipped threshold |
| ROC-AUC | 0.9759 | threshold-free |
| PR-AUC | 0.8216 | threshold-free, but see §5.4 |
| F1 | 0.9999 | **do not report** — test-set-searched threshold |
| point-wise F1 | *unmeasured* | all figures above are point-adjusted |

The notebook's own comment: *"the best-threshold 'F1' column can read ~1.0 even for a broken
model, so it must NOT be used to rank or to report."* Publishing 0.9999 would not survive
scrutiny. Additionally, all figures above use `POINT_ADJUST: True`, which per Kim et al.
(cited in the project's own supervisor material) systematically overstates performance.
**Raw point-wise F1 at this threshold is unmeasured and will be materially lower.**

### 5.2 The data defect

`normal.csv` has two structurally distinct blocks:

- **Rows 1–395,298** — the `Normal`-labelled rows of the attack period (28/12 → 02/01).
  `449,919 − 54,621 = 395,298`, i.e. exactly the classic `Attack_v0` file. **No missing values.**
- **Rows 395,299–1,387,098** (991,800) — the 22/12 → 28/12 normal run, **present twice**
  (496,800 rows from 16:00, then 495,000 rows from 16:30; byte-identical on all 495,000
  shared timestamps). This is `Normal_v0` + `Normal_v1`, iTrust's original and corrected
  re-release, concatenated.

The seven leading-space columns are **empty in all 991,800 block-2 rows** — a header-spelling
mismatch when the attack file (`" MV101"`) was concatenated with the normal files (`"MV101"`).
The notebook's `.ffill().fillna(0)` then silently carries block 1's last value forward.

Six of those seven are **model inputs** (`P202` was dropped by `VAR_THRESHOLD`):

| Feature | distinct values in TRAIN | in VALIDATION | frozen at |
|---|---:|---:|---|
| `AIT201` | 1,914 | **1** | 168.0979 (z = −0.516) |
| `MV101` | 3 | **1** | 2.0 |
| `MV201` | 3 | **1** | 2.0 |
| `P201` | 2 | **1** | 2.0 |
| `MV303` | 3 | **1** | 1.0 |
| `P204` | 1 | **1** | 1.0 |

- **64.4%** of training rows affected (142,877 / 221,936)
- **100%** of validation rows affected (55,483 / 55,483) — the validation split falls entirely
  inside block 2

### 5.3 Consequence

The shipped threshold was calibrated on data where **6 of 45 model input features (13%) carry
zero variance and physically incorrect values** — most starkly `AIT201`, a real conductivity
analyzer with 1,914 distinct training values, pinned to a single number.

On complete telemetry those six features vary, the input distribution shifts away from the
calibration distribution, and **the threshold's by-construction 1% false-positive rate will
not hold. Expect elevated false positives.**

This also explains the zero-variance features noted in the config: `P204`'s `var_ = 0` is an
artifact of this gap (empty in block 2, constant 1.0 in block 1), **not** a physical constant.
`P206` is separately and genuinely constant in normal operation.

### 5.4 Secondary effect on reported metrics

Because 35.7% of normal rows are exact duplicates, the attack ratio in the concatenated data
is **3.79%** rather than the 12.14% of the canonical attack file. PR-AUC is prevalence-
sensitive, so the reported **PR-AUC 0.8216 was computed at an artificially deflated base
rate.** Separately, the notebook evaluates on *all* data (`Xte = scaler.transform(X.values)`),
so reported metrics include training rows — compounded here by the duplication.

### 5.5 Current decision

**Ship the existing threshold unchanged; document the caveat.** `threshold.json` is not
modified and the model is not retrained.

Recalibration on a clean validation subset (drawn from block 1, where all 45 features are
populated) is a **deferred follow-up requiring explicit approval** — it would write a new
sibling artifact, never modify the existing one, and would not involve retraining. Any
figure published from the current threshold must carry the §5.3 caveat.

### 5.6 Constant features: the training split and the clean slice answer different questions

A regression test (`tests/test_dataset.py::test_constant_features_are_reported`)
originally assumed the clean Attack_v0 slice's constant features would be a subset
of the scaler-frozen set `{P204, P206}`. They are not. Measured over the clean
slice, the **only** single-valued feature is **`P301`** (constant `1.0`). This was
verified independently of the loader by scanning the raw CSVs directly:

| Feature | `attack.csv` (54,621 rows) | `normal.csv` rows 1..395,298 | clean Attack_v0 (both, attacks incl.) | `scaler.joblib` `var_` (training) |
|---|---|---|---|---|
| `P301` | `1.0` only | `1.0` only | **constant `1.0`** | `0.00337` (mean `1.0034`) — **not frozen** |
| `P204` | `{1.0, 2.0}` | `1.0` only | varies (`2.0` under attack) | `0.0` — **frozen** |
| `P206` | `{1.0, 2.0}` | `1.0` only | varies (`2.0` under attack) | `0.0` — **frozen** |

Two different distributions, two different answers:

- **`scaler.joblib` froze `{P204, P206}`.** It was fit on the normal-only training
  split of the *research concat*, which includes the block-2 duplicate. There P204
  is empty→ffilled to a constant (the §5.2 gap) and P206 is genuinely constant in
  normal operation. This matches `test_p204_p206_have_zero_variance_in_training`.
- **The clean slice's only constant is `P301`.** P204 and P206 are constant only in
  *normal* operation; both actuate to `2.0` during attacks, and the clean slice
  contains attacks, so they vary. P301 is `1.0` across every row of both source
  files in the 2015-12-28 → 2016-01-02 range this slice covers; the scaler did not
  freeze it because it *does* reach `2.0` elsewhere in the fuller normal record.

This is a **data-characterization finding, category (B/C)** — an incomplete test
assumption over a genuinely different distribution. It is **not** a reconstruction
defect: the reconstruction faithfully reproduces the raw rows. It touches no
artifact, the scalers, the model, or the threshold. The regression test now asserts
the measured set exactly (`== {"P301"}`), which is a stronger guard than the
original subset check. It also resolves the §5.3 open question for P204: the loader
confirms P204 is not a physical constant (it varies under attack), so its
training-time zero variance was the block-2 gap.

---

## 6. Replay dataset for the demo

**Do not use `merged.csv`** — it carries the identical 991,800-row gap.

**Use the reconstructed clean `Attack_v0`:** `attack.csv` (54,621 rows) + `normal.csv` rows
1–395,298 = **449,919 rows, zero missing values, all 45 features populated**, at the true
**12.1402%** attack ratio. Merge by `Timestamp` to restore the original
normal → attack → normal sequence (attack labels span 2015-12-28 10:29:14 → 2016-01-02 13:41:11).

The labels form **35 contiguous attack segments** (measured: gap > 1 s between sorted attack
timestamps). The project proposal states "36 labelled attack scenarios"; the SWaT literature
describes 36 attacks *launched*. The difference is expected — back-to-back attacks merge into
one contiguous label run, and some launched attacks produced no labelled physical effect.
**Cite 35 when describing this dataset's labels, and 36 only when describing attacks
launched on the testbed.**

Raw `attack.csv` alone is unsuitable for replay: it is label-filtered, so its attack segments
are concatenated with time discontinuities and do not represent a realistic telemetry stream.

**Replay cadence** follows from `SUBSAMPLE = 5`:

| Quantity | Value |
|---|---|
| Source rate | 1 Hz |
| Effective sample period | **5 s** |
| Window span (30 × 5 s) | **150 s** |
| Buffer fill time / detection latency floor | **150 s** |

> 150 s is a hard floor. The project's earlier supervisor material described *"scoring a
> single reading in microseconds"* — that is not what this model does. A single reading is
> not scoreable; TranAD requires a filled 30-sample window. Plan the demo with a
> real-time-factor knob so a run completes in minutes rather than hours.

---

## 7. Open items

| # | Item | Blocking | Status |
|---|---|---|---|
| 1 | Pin the `torch` version and validate with a golden-vector test | Phase 1 | **done** — `==2.9.1`, `tests/test_golden_vectors.py` |
| 2 | Threshold recalibration decision (§5.5) | Phase 4 / 6 honesty | deferred pending item 3 |
| 3 | Measure **point-wise** precision/recall/F1 at the shipped threshold | Phase 6 | **approved** — do before item 2 |
| 4 | Regression test: assert the six §5.2 features are not silently frozen in live input | Phase 1 | **partial** — non-finite input is rejected, but a channel frozen at a *plausible* constant still passes silently. Needs a stuck-channel check in the replay/ingest layer. |
| 5 | Regression test: assert C-order flattening (silent-corruption class) | Phase 1 | **done** — `test_fortran_flatten_changes_score` |
| 6 | Notebook metrics tables are not in version control — only in external meeting material | Phase 0.4 | open |

---

## 8. Reproducing this verification

Read-only, no third-party dependencies (pure Python; `numpy`/`torch` not required):

1. Read the header from `attack.csv`; strip whitespace from column names.
2. Concatenate `attack.csv` then `normal.csv` (excluding `merged.csv`), applying pandas
   `ffill().fillna(0.0)` semantics per column.
3. Compute each column's sample std (`ddof=1`) over all rows; keep `std > 1e-8` → expect
   45 features matching `feature_names.json` in order.
4. Take rows `::5`; select `Normal`-labelled; take the first `n − int(0.2n)` in order →
   expect exactly 221,936 rows.
5. Compute population `mean`/`var` (`ddof=0`) with a **two-pass** algorithm; compare against
   `scaler.joblib` (float64 at offsets 448 and 864).
6. Apply `z = (x − mean) / scale`; take per-feature min/max over the training rows; compare
   against `minmax_scaler.joblib` `data_min_`/`data_max_` (float32 at offsets 11296 and 16768).

Artifact SHA-256 checksums are recorded in
[`ml/configs/tranad_swat.yaml`](../../ml/configs/tranad_swat.yaml) under `artifacts.sha256`;
verify them before trusting any parity run.
