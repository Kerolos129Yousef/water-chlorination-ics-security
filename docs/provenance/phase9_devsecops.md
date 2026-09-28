# Phase 9 - CI/CD + DevSecOps Security Gates

**Status:** implemented on branch `phase9-devsecops`, baseline `main` @ `5baeb58`.
**Scope:** validation pipeline only. No cloud deploy, no registry publishing, no
ML/threshold/dataset changes.

---

## 1. Objective

Add a **real, executable** GitHub Actions pipeline that automatically validates
the repository before changes reach `main`. It enforces six security/quality
gates plus a real Docker build + image scan, so a change cannot merge unless it:

1. passes code-quality (lint / static checks),
2. passes the full test suite,
3. passes Python static-security analysis (SAST),
4. has no unaccepted dependency vulnerabilities,
5. leaks no secrets, and
6. produces a container image with no unaccepted HIGH/CRITICAL vulnerabilities.

The pipeline is **not decorative**: every gate runs the actual tool against the
actual code/image and fails the build on genuine findings. This was verified by
running each tool locally (evidence in section 12) before wiring the workflow.

---

## 2. Pipeline architecture

Single workflow: [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).

```
             push -> main   /   pull_request -> main
                        |
        +---------------+----------------------------------------+
        |        |        |        |          |          |       |
     lint     tests     sast     deps      secrets   container   |
    (ruff)   (pytest   (bandit) (pip-     (gitleaks) (docker     |
             + PG16)            audit)               build+trivy) |
        |        |        |        |          |          |       |
        +---------------+----------------------------------------+
                        |
                    ci-gate  (needs: all six; if: always())
                 fails unless every required gate == success
```

The six gate jobs run **in parallel**. A final `ci-gate` job depends on all of
them and re-asserts that each reported `success`; it is the single required
status check to protect `main` with. Using `if: always()` + explicit `result`
checks means a skipped or cancelled gate cannot masquerade as a pass.

---

## 3. CI environment

| Property | Value | Rationale |
|---|---|---|
| Runner | `ubuntu-latest` (Linux) | Canonical environment for the golden-vector fixtures (section 10). |
| Python | 3.10 | Matches the verified Phase 0-8 environment (`python:3.10-slim` image, cp310 wheels). |
| torch install | CPU wheel index (`download.pytorch.org/whl/cpu`) | ~200 MB vs ~2.5 GB CUDA; identical numerics (2.9.1+cpu). |
| Dependency source | `docker/requirements-runtime.txt` (production pins) + pinned test tools | Reproducibility over speed; no unpinned installs. |
| Caching | `actions/setup-python` pip cache keyed on the requirements files | Speed without sacrificing pinned reproducibility. |

Production dependencies are installed from the exact pinned manifest the image
uses. CI-only tooling (ruff, bandit, pip-audit, gitleaks, pytest, httpx) is
installed separately and pinned, and never added to production requirements.

---

## 4. Gates, tools, and fail policy

### Gate 1 - Code quality: **ruff 0.16.9**
- Command: `ruff check --select E9,F backend alerting simulator ml/src tests`
- **Fails on:** genuine defects only - pyflakes (`F`: undefined names, unused
  imports, f-string bugs, redefinitions) and syntax errors (`E9`).
- **Why ruff, why this rule set:** the repo had no linter configured. Ruff is a
  fast, standard, zero-config Python linter. We deliberately restrict to
  correctness rules (not cosmetic style) so the gate catches real bugs **without
  forcing an application rewrite** to satisfy a newly introduced style tool
  (explicit project constraint). Current result: `All checks passed!`

### Gate 2 - Tests: **pytest 8.4.2**
- Command: `python -m pytest -q -rs --durations=10`
- **Fails on:** any test failure.
- **PostgreSQL:** a `postgres:16-alpine` **service container** is provided (same
  major version as the project); `ALERT_TEST_PG_DSN` points the Phase 8B suite at
  it with **ephemeral CI-only credentials**. See section 7.
- **SWaT dataset:** not present in CI (licensed, local). `SWAT_DATASET_DIR` is set
  to a non-existent path so the 16 dataset-gated tests **skip with an explicit
  reason** via the `swat_dataset_dir` fixture. CI never downloads, fabricates,
  commits, or logs the dataset. Dataset-independent tests are **not weakened** to
  compensate.
- **Golden vectors:** run unchanged on Linux (the canonical fixture host). See
  section 10.
- Expected CI outcome: **411 collected, ~395 passed, 16 skipped, 0 failed**
  (local full run with dataset + Docker present: 411 passed).

Dataset-gated tests (skip in CI): 16 tests across `tests/test_dataset.py` and
`tests/test_replay.py`, marked/guarded by the `swat_dataset_dir` fixture.

### Gate 3 - SAST: **bandit 1.9.4**
- Command: `bandit -r backend alerting simulator ml/src --severity-level medium
  --confidence-level high`
- **Fails on:** findings at **severity >= MEDIUM AND confidence >= HIGH**.
- **Current result:** `No issues identified` at that bar -> clean pass, **no
  suppressions required**. Lower-tier findings are reviewed and documented rather
  than globally silenced:
  - `B608` (x4, `alerting/store.py`) "possible SQL injection" - **Medium/Low**.
    False positives: the SQL is built from the module constant `_COLUMNS` and a
    fixed table name; **all user values are bound with `%s` parameters**. Not
    fixed by suppression - simply below the confidence bar.
  - `B105` (`alerting/store.py:63`) "possible hardcoded password" - **Low/Medium**.
    False positive: the flagged literal is the **env-var name** `"POSTGRES_PASSWORD"`,
    not a password value.
  - `B110` (`alerting/store.py:514`) try/except/pass - **Low/High**. Intentional
    best-effort pool teardown, already annotated `# noqa: BLE001`; Low severity,
    so below the bar.
- No directory is globally excluded; no blanket `#nosec`.

### Gate 4 - Dependency security: **pip-audit 2.10.1**
- Command:
  ```
  pip-audit -r docker/requirements-runtime.txt --no-deps \
    --ignore-vuln PYSEC-2026-139 --ignore-vuln PYSEC-2025-194 \
    --ignore-vuln PYSEC-2025-195 --ignore-vuln PYSEC-2026-2286
  ```
- **Fails on:** any advisory against the pinned production runtime manifest,
  except the four documented torch exceptions below.
- **Why `--no-deps` against the runtime manifest:** `docker/requirements-runtime.txt`
  is the single source of truth for what ships in the image and is already a
  fully-resolved pin list. Auditing it directly (a) covers the real production
  dependencies, (b) does not silently resolve/upgrade anything in CI, and (c)
  audits `torch==2.9.1` by its canonical version (the installed image build tags
  it `2.9.1+cpu`, a local version that PyPI/the advisory DB cannot match, so an
  environment scan would silently *skip* torch - this method does not).
- **Documented, time-bounded torch exceptions** (see section 11). Any NEW advisory
  (torch or otherwise) still fails the gate - this is not a blanket ignore.

### Gate 5 - Secret scanning: **gitleaks 8.30.1**
- Install: pinned release binary, **SHA-256 verified against the published
  `checksums.txt`** (no third-party action, no license token).
- Command: `gitleaks detect --source . --no-banner --redact --verbose` with
  `fetch-depth: 0` (full history).
- **Fails on:** any gitleaks detection.
- **Current result:** `no leaks found` across all 15 commits (854 KB scanned).
- **No custom allowlist added.** `.env.example` ships only the documented
  `change-me-to-a-strong-password` placeholder and non-secret DB name/user;
  gitleaks does not flag these. `_PG_PASSWORD = "test-only-password"` in
  `tests/test_postgres_alert_store.py` is an ephemeral test-container literal and
  is likewise not flagged. No real secret is committed.

### Gate 6 - Container security: **Docker build + Trivy**
- Build: `docker build -f docker/backend.Dockerfile -t ics-guardian-backend:ci .`
  (the **real production Dockerfile**; frontend image built too). No CI-only
  Dockerfile. The SWaT dataset is excluded by `.dockerignore` and never enters
  the build context or any layer.
- Scan: `aquasecurity/trivy-action` (pinned SHA), `scanners: vuln`,
  `severity: HIGH,CRITICAL`, `ignore-unfixed: true`, `exit-code: 1`,
  `trivyignores: .trivyignore`.
- **Fail policy:**
  - **CRITICAL (fixable): fail.** Current count: 0.
  - **HIGH (fixable): fail**, except the documented [`.trivyignore`](../../.trivyignore)
    exceptions.
  - **Unfixed OS/base-image CVEs: reported, not blocking.** Detection stays fully
    ON (HIGH+CRITICAL are scanned and printed). The Debian base contributes ~45
    HIGH findings that are **unfixed upstream** (`FixedVersion: None`, e.g.
    `util-linux`, `perl-base`, `libsystemd0`, `ncurses`). Blocking on them would
    force either disabling detection or forging a base image - both forbidden.
    Remediation belongs to the base image and is scheduled for Phase 10
    (hardened/distroless base).
- **Current result with policy:** Trivy exit `0` (clean) - the only fixable HIGH
  findings are the four documented pip/setuptools-vendored exceptions.

---

## 5. Exact fail policy (summary table)

| Gate | Tool | Blocks the build when... | Current |
|---|---|---|---|
| Lint | ruff 0.16.9 | any `E9`/`F` finding | pass |
| Tests | pytest 8.4.2 | any test fails | pass (dataset tests skip) |
| SAST | bandit 1.9.4 | severity>=MEDIUM AND confidence>=HIGH | pass |
| Deps | pip-audit 2.10.1 | any advisory except 4 documented torch pins | pass |
| Secrets | gitleaks 8.30.1 | any detection (full history) | pass |
| Container | Trivy (action v0.36.0) | any fixable HIGH/CRITICAL except 4 documented `.trivyignore` | pass |

---

## 6. Triggers, permissions, concurrency

- **Triggers:** `pull_request` -> `main` and `push` -> `main`. No feature-branch
  trigger (avoids recursive/duplicate runs; PRs already cover feature branches).
- **Permissions:** workflow-level `permissions: contents: read` only. No job
  writes, comments, or publishes; the default `GITHUB_TOKEN` never gains extra
  scope, and no secrets are exposed to any job.
- **Concurrency:** `cancel-in-progress` per ref to save minutes; the final gate
  re-evaluates all required jobs so cancellation cannot mask a failure.

---

## 7. PostgreSQL CI strategy

Phase 8B integration tests exercise the **real** SQL path (mocking would defeat
the point). In CI they run against a `postgres:16-alpine` **service container**
(matching the project's Postgres 16 major). The suite's `pg_dsn` fixture reads
`ALERT_TEST_PG_DSN`; CI sets it to the service using **ephemeral, CI-only
credentials** (`ci-ephemeral-not-a-secret`). No external/cloud Postgres, no
hard-coded production credentials, no committed secret. If the service were
unavailable the suite would fall back to spawning its own container and, failing
that, skip - so the pipeline degrades safely.

---

## 8. Dataset exclusion (no SWaT leak)

- CI never has the licensed SWaT CSVs; `SWAT_DATASET_DIR=/nonexistent-...` makes
  the 16 dataset-gated tests skip with an explicit reason.
- `.gitignore` (`*.csv`) and `.dockerignore` (`*.csv`, `tests/`, dataset/archive
  patterns) keep the dataset out of version control and out of every image layer.
- CI does not download, fabricate, upload as an artifact, bake into an image, or
  print the dataset. No `upload-artifact` step exists.

---

## 9. Supply-chain / action pinning

Third-party actions are pinned to **immutable commit SHAs** (moving tag recorded
in a trailing comment for readability):

| Action | Version | Pinned SHA |
|---|---|---|
| `actions/checkout` | v4.4.0 | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` | v5.6.0 | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `aquasecurity/trivy-action` | v0.36.0 | `a9c7b0f06e461e9d4b4d1711f154ee024b8d7ab8` |

Gitleaks uses **no action** at all - a pinned release binary with a verified
SHA-256 checksum - to minimise third-party action surface and avoid the
`gitleaks-action` license requirement.

---

## 10. Golden-vector requirement

The golden-vector tests (`tests/test_golden_vectors.py`, fixtures in
`tests/fixtures/golden_vectors.json`) are **unchanged**: no widened tolerances,
no regenerated fixtures, no skip, no xfail. They run on the `ubuntu-latest`
runner, establishing Linux as the canonical CI parity environment. The
previously documented **Windows-only ~1.7 ppm numerical drift** remains a
host-environment issue; no new evidence disproves that diagnosis, and running the
canonical checks on Linux is the correct response.

---

## 11. Known exceptions (justified, narrow, time-bounded)

### 11.1 torch 2.9.1 - four pip-audit advisories (Gate 4)

| Advisory | Aliases | Fix | Vector | Reachable here? |
|---|---|---|---|---|
| PYSEC-2026-139 | CVE-2026-4538 | (none) | pt2 loading-handler deserialization, **local** | No |
| PYSEC-2025-194 | CVE-2025-3000 | 2.13.0 | `torch.jit.script` memory corruption, **local** | No - not used |
| PYSEC-2025-195 | CVE-2025-3001 | 2.10.0 | `torch.lstm_cell` memory corruption, **local** | No - not used |
| PYSEC-2026-2286 | CVE-2026-24747 | 2.10.0 | `weights_only` unpickler on a **crafted checkpoint** | No |

**Justification.** All four require a local vector and either an untrusted model
file or a torch API this service never calls. The service loads exactly one
**immutable, version-controlled** artifact (`ml/artifacts/swat_TranAD/model.pt`)
with `weights_only=True`, on a **read-only, non-root** container; there is no code
path that loads an attacker-supplied checkpoint, and `torch.jit.script` /
`torch.lstm_cell` are not used (TranAD is a `TransformerEncoder`, `batch_first=False`).
The network surface is the numeric `/score` endpoint (pydantic-validated), never a
model file.

**Why not just upgrade.** torch is pinned to **2.9.1 by the golden-vector
constraint** (fixtures were generated under it; see `ml/requirements.txt`). Fixes
land in 2.10.0 / 2.13.0, which would change numerics and require regenerating and
re-verifying the golden vectors and detection metrics - explicitly **out of scope
for Phase 9** (no model changes/recalibration). **Follow-up (Phase 10):** evaluate
torch >= 2.13.0, re-run golden vectors, and remove these ignores.

### 11.2 Trivy `.trivyignore` - four pip/setuptools-vendored advisories (Gate 6)

`CVE-2025-47273` (setuptools), `CVE-2026-24049` (wheel), `CVE-2026-23949`
(jaraco.context), `GHSA-6v7p-g79w-8964` (msgpack).

**Justification.** These are flagged from **vendored dist-info metadata inside
pip/setuptools** (`pip/_vendor/msgpack`, `setuptools/_vendor/{wheel,jaraco.context}`,
a vendored setuptools dist-info) - copies used only by the **package installer**.
They are not imported by `uvicorn backend.app:app`, and pip is **never invoked** in
the immutable, read-only, non-root runtime container. The importable setuptools in
the image is already `79.0.1` (patched); Trivy reports a stale vendored copy. All
exploit vectors require installing untrusted packages/wheels/tars at runtime, which
never happens. **Follow-up (Phase 10):** move to a base that ships no
pip/setuptools in the runtime layer, then delete these entries and re-scan.

### 11.3 Unfixed base-OS CVEs (Gate 6)

Reported but not blocking (`--ignore-unfixed`): ~45 HIGH findings in the Debian
`python:3.10-slim` base (`util-linux`/`libmount1`/`libblkid1`/`libuuid1`,
`perl-base`, `libsystemd0`/`libudev1`, `ncurses*`) with **no upstream fix**.
Remediation is a base-image change (Phase 10).

---

## 12. Test evidence (local, pre-commit, Linux)

All tools were run locally before wiring the workflow. Verbatim outcomes:

```
$ ruff check --select E9,F backend alerting simulator ml/src tests
All checks passed!

$ python -m pytest -q                          # dataset + Docker present locally
411 passed, 1 warning in 65.17s

$ SWAT_DATASET_DIR=/nonexistent python -m pytest -q   # CI-like (no dataset, PG excluded)
376 passed, 16 skipped, 1 warning               # +postgres suite via service in CI

$ bandit -r backend alerting simulator ml/src --severity-level medium --confidence-level high
No issues identified.                            # (Low:2 Medium:4 High:0 total, all below bar)

$ pip-audit -r docker/requirements-runtime.txt --no-deps                 # no ignores
Found 4 known vulnerabilities in 1 package  (torch 2.9.1)  -> exit 1
$ pip-audit -r docker/requirements-runtime.txt --no-deps --ignore-vuln ... (x4)
No known vulnerabilities found, 4 ignored        -> exit 0

$ gitleaks detect --source .
15 commits scanned. no leaks found               -> exit 0

$ docker build -f docker/backend.Dockerfile -t ics-guardian-backend:ci .
BUILD_EXIT=0                                      # image ~1.66 GB (CPU torch)

$ trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
        --ignorefile .trivyignore ics-guardian-backend:ci
TRIVY_EXIT=0                                      # 0 CRITICAL; 4 vendored HIGH excepted
```

The **1 warning** is the pre-existing Starlette/httpx `TestClient` deprecation
warning; unchanged by Phase 9.

The **real GitHub Actions run** result is recorded in section 13 of the final
report / PR after the first push.

---

## 13. Local reproduction commands

```bash
# From the repo root, inside the project venv (Python 3.10).
pip install ruff==0.16.9 bandit==1.9.4 pip-audit==2.10.1 pytest==8.4.2

# Gate 1 - lint
ruff check --select E9,F backend alerting simulator ml/src tests

# Gate 2 - tests (dataset skips automatically if absent; Docker gives you PG)
python -m pytest -q -rs

# Gate 3 - SAST
bandit -r backend alerting simulator ml/src --severity-level medium --confidence-level high

# Gate 4 - dependency audit
pip-audit -r docker/requirements-runtime.txt --no-deps \
  --ignore-vuln PYSEC-2026-139 --ignore-vuln PYSEC-2025-194 \
  --ignore-vuln PYSEC-2025-195 --ignore-vuln PYSEC-2026-2286

# Gate 5 - secrets (via pinned container; or install the binary)
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.30.1 \
  detect --source=/repo --no-banner --redact

# Gate 6 - build + scan
docker build -f docker/backend.Dockerfile -t ics-guardian-backend:ci .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.cache/trivy:/root/.cache/trivy" -v "$PWD/.trivyignore:/.trivyignore" \
  aquasec/trivy:0.74.0 image --scanners vuln --severity HIGH,CRITICAL \
  --ignore-unfixed --exit-code 1 --ignorefile /.trivyignore ics-guardian-backend:ci
```

---

## 14. Integrity guarantees

Phase 9 touches **no** ML artifact, threshold, scaler, feature list, or model
code, and **no** application semantics (TranAD, telemetry-health, AlertEngine,
persistence). Verify with `git diff --stat` against `5baeb58`: changes are
confined to `.github/workflows/ci.yml`, `.trivyignore`, this doc, and the
`README`/`PROJECT_CONTEXT`/`PROJECT_HANDOFF` narrative updates.
