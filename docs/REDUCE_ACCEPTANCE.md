# REDUCE V1: Scientific Acceptance (second pass)

This document records the second-pass validation/hardening/measurement/
comparison campaign against `reduce_engine` as it stood at commit
`9cf7dd7`, and the evidence behind the FREEZE decision in
`docs/REDUCE_V1_FREEZE.md`.

## Audit of 1b2d014..9cf7dd7 (before any new work)

Grepped for: hardcoded paths/campaign ids, network access, hardware
imports, side effects, implicit randomness, timezone assumptions,
magic frequency numbers, silent NaN handling, silent fallback behavior.

Findings:
- No hardware imports, no network imports (`requests`/`urllib`/sockets),
  no magic frequency numbers outside config/`hi_spectral_metric.py`, no
  randomness outside `reduce_engine/simulation.py` (as expected - it is
  the synthetic simulator).
- **Real gap found**: `pipeline.py`'s integration-time lookup
  (`attrs.get("duration_seconds", attrs.get("capture_time_seconds", 0.0))
  or 0.0`) silently defaulted to `0.0` with no quality flag when both
  source attributes were absent. **Fixed**: `quality.assess_quality` now
  takes `integration_time_known` and reports a WARNING reason when the
  value had to be defaulted (`quality.py`, `pipeline.py`).
- **Real gap found**: a NaN `baseline_fit_quality_rms_fraction` (fit
  could not be evaluated at all) passed through `assess_quality`
  untouched and could read as GOOD - "unknown" is not "fine". **Fixed**:
  NaN baseline RMS now triggers an explicit WARNING reason.
- **Real security gap found** (same class of bug ALIGN/CALIBRATE's web
  layer found in the first pass): `storage.ReduceSession` built its
  output path by concatenating `output_root / campaign_id / session_id`
  with no validation - `campaign_id` comes from `grid_metadata.json`'s
  `session_name`, read from inside the campaign directory but not
  actually trusted-by-construction (a corrupted or malformed file could
  contain `"../../etc"`). **Fixed**: `_reject_unsafe_path_component()`
  rejects any campaign/session id containing `/`, `\`, `..`, or a null
  byte, and the resolved path is re-checked to remain under
  `output_root` (covers symlinked roots too). See
  `test_reduce_timezone_and_security.py`.
- No hidden global state, no bare `except:` clauses (the one broad
  `except Exception` in `pipeline.reduce_point` is deliberate and
  documented: a point's failure must never abort the campaign).

## Real campaigns tested (never the active campaign)

`data/mosaic/ALMITA-OBSERVE-20260917-20:10:26` was confirmed RUNNING
during this pass (`quicklook_live.py` PID 259874, `rtl_tcp` PID 858 both
alive against it) and was never opened, read, or processed.

| id | campaign | points | notes |
|---|---|---|---|
| A (small) | `ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16` | 9/9 success | known-good baseline from the first pass |
| B (medium) | `ALMITA-OBSERVE-20260914-22:05:06` | 100/100 success | clean finished campaign; also this pass's performance-measurement campaign |
| C (messy) | `ALMITA-OBSERVE-20260902-16:56:58` | 50/100 success (50 "planned", never captured) | deliberately NOT hand-picked for a clean pass - a real, historically stopped-early campaign |

All three: RAW byte-identical (SHA-256 of every file under the campaign
directory) before and after REDUCE ran against them
(`test_reduce_multi_campaign_real.py`). C correctly reports session
status `PARTIAL` (never `COMPLETED`) with every one of the 100 planned
points listed in the manifest as either `COMPLETED` or `BLOCKED` - none
silently dropped.

## MasterSpectrum V1 contract

See `docs/REDUCE_MODEL.md`'s `## MasterSpectrum` section for the field
table and `## Units contract (V1, definitive)` for units - unchanged
from the first pass's schema, hardened with a units table and an
explicit schema-versioning policy. `reduce_schema_version` remains
`"1.0"`; a document naming a policy for `"1.x"` vs `"2.0"` changes is now
in `docs/REDUCE_MODEL.md`.

## Calibration matrix

| case | input | result |
|---|---|---|
| A. compatible | real profile + real matching capture | `calibration_level=RELATIVE`, `compatibility_status=COMPATIBLE` |
| B. absent | no profile supplied | `calibration_level=UNCALIBRATED`, `compatibility_status=NOT_ATTEMPTED`, explicit reason |
| C. incompatible (sample rate) | real profile + synthetic capture with 2x sample_rate | `UNCALIBRATED`, `INCOMPATIBLE`, reason names "sample rate" |
| C. incompatible (gain) | real profile + synthetic capture with +10dB gain | `UNCALIBRATED`, `INCOMPATIBLE`, reason names "gain" |

Never silently applied when incompatible; the reason always survives into
`quality.reasons` (`test_reduce_calibration_matrix.py`).

## Baseline validation

Adversarial scenarios (`test_reduce_baseline_adversarial.py`): broad
HI-like feature, narrow line, two blended components, strong slope,
ripple, 60%-masked band, RFI 40 kHz from the line window. All 8 pass: the
baseline never eats an injected line below 30% of its expected integral,
never fits a false dip in a genuine gap between two components, and
raises `ValueError` (never a garbage fit) when truly too few bins remain.

## Velocity independent cross-check

`test_reduce_velocity_crosscheck_and_offline.py` compares
`reduce_engine.velocity`'s LSRK output against `SkyCoord.
radial_velocity_correction("barycentric")` - a genuinely different
astropy entry point - across 5 dates/positions. Median disagreement
< 25 km/s, max < 30 km/s (the expected order of the Sun's own ~20 km/s
peculiar motion relative to the barycentric frame, not error). Same file
proves the IERS-offline fix holds: `auto_download=False`,
`auto_max_age=None` confirmed set, and a full velocity conversion
succeeds with `socket.socket.connect` monkeypatched to raise.

## Resampling / averaging / stacking

- Resample: a masked gap in a source grid, interpolated onto a finer
  common grid, is never presented as `GOOD`/confident data
  (`test_reduce_resample_and_doppler_stability.py`).
- Averaging real QA: RMS vs N (1/2/4/8/16 captures) stays within a
  0.2x-5x band of the `1/sqrt(N)` theoretical reference - reported as a
  diagnostic, never asserted as exact.
- Robust stacking stress test: 6 pathologies (gain shift, broadband
  offset, narrow RFI, wrong baseline, clipping, partial data) against 15
  sane spectra - never worse than a naive mean, substantially better in
  4/6 (the other 2 legitimately converge with naive nan-aware handling).
- **Real bug found and fixed during this exact testing**: the original
  sigma-clip used the weighted mean/variance being solved for as its own
  outlier-detection reference - a single extreme pathological spectrum
  inflated that reference enough to escape detection ("swamping").
  Replaced with a robust median/MAD detector.
  (`test_reduce_averaging_realqa_and_stress.py`,
  `reduce_engine/averaging.py`)
- **Real bug found and fixed**: `method="equal_weight"` still silently
  filtered by uncertainty finiteness (inherited from the inverse-variance
  path's `usable` mask), so an all-NaN-uncertainty equal-weight request
  produced an all-zero-weight, all-NaN result instead of a plain mean.
  Fixed by computing `usable` per-method.

## Uncertainty coverage

40-realization Monte Carlo of a known noise-only signal
(`test_reduce_uncertainty_coverage.py`). Median (measured cross-
realization scatter / reported per-capture uncertainty) ratio in [0.3,
3.0] - not exactly 1 (a real, understood systematic exists: see below),
but nowhere near orders-of-magnitude off. +-1 sigma coverage measured
against the empirical population mean, not a theoretical target (see
finding below): between 35% and 95%; +-2 sigma between 65% and 99.9%.

**Real, non-code finding**: `hi_spectral_metric.robust_psd_from_iq`'s
default `combine="median"` is a *biased* estimator of the true Gaussian
noise variance (the median of a chi-squared-like per-bin power
distribution sits well below its mean) - using the simulator's
theoretical `noise_level` as "truth" made coverage compute to exactly
0.00 before this was understood. Corrected the test to use the empirical
cross-realization mean as ground truth. This bias is self-consistent and
invisible in REDUCE's own relative/fractional outputs (it cancels in any
ratio-to-baseline or ratio-to-reference-profile computation) - it only
matters if something someday treats a raw PSD level as an absolute
number, which REDUCE never does.

## Quality adversarial matrix / false-GOOD / false-BAD

11 scenarios (`test_reduce_quality_adversarial_matrix.py`): healthy,
moderate RFI, extreme RFI, heavy mask, bad baseline, no calibration,
missing velocity, single capture, clipping, no contributing spectra, NaN
baseline RMS.

**FALSE-GOOD count: 0. FALSE-BAD count: 0.**

(One fix was needed to reach this: NaN baseline RMS was reading GOOD
before the fix described above.)

## Replay determinism / compare validation / provenance

- Replay of a real 9-point session: every science array
  (`frequency_hz`, `relative_intensity`, `uncertainty`, `mask`,
  `n_contributing`, `velocity_lsrk_m_s`) numerically identical
  (`equal_nan=True` - masked/edge bins are legitimately NaN, never
  zero-filled, and matching NaN-for-NaN across runs IS the determinism
  being tested). Quality state and calibration level identical.
  Timestamps/session ids differ, as designed.
- Compare: (A) same-session replay -> RMS difference exactly 0.0 for
  every point; (B) same raw, different `fft_size` -> difference
  correctly identified in `differing_config_keys`; (C) same raw/config,
  different calibration -> nonzero measured RMS difference, quality
  counts differ; (D) different declared source campaign -> correctly
  flagged `same_source_campaign=False`. Never reduces to a bare "files
  differ" - always structured fields.
- Provenance chain: one real point (`ALMITA-WEB-SMALL-RUN-01`, point 1)
  traced from `master_spectrum.json` -> `reduce_session_id` -> config
  hash (verified equal to `ReduceConfig(**config.json).config_hash()`)
  -> calibration profile id/hash -> `CaptureRef` -> raw HDF5 path (exists
  on disk) -> SHA-256 (re-hashed live and matched the recorded value).

## Edge semantics

- Single-capture point: `n_contributing == 1` everywhere, uncertainty is
  honestly that one capture's own estimate (never artificially shrunk).
- **Documented, not fixed, gap** (mixed configuration / duplicate
  capture): `stack_spectra` has no compatibility gate and no identity
  awareness - it will blindly average a wildly different-amplitude
  "capture" or double-count a literally duplicated array. This is a real
  gap in the *primitive*, but it is **not reachable** through the real
  V1 pipeline today because `reduce_engine.ingest` maps exactly one
  capture to one point (confirmed true for every real campaign audited -
  `mosaic.csv`'s `point_number` is unique per row everywhere). Both gaps
  are covered by tests that pin today's exact (gap) behavior, so a
  future multi-capture-per-point INGEST cannot silently reintroduce them
  unnoticed - see `docs/REDUCE_V1_FREEZE.md`'s known limitations.
- Out-of-order captures stack to the identical result regardless of
  input order; `discover_campaign` follows `mosaic.csv`'s own row order
  (never `os.listdir()`), confirmed stable across 5 repeated calls
  against a real campaign.

## Timezone / offline / security

- 4 timestamp variants of the same physical instant (`+00:00`, `Z`, no
  fractional seconds, a non-UTC offset) all agree to within 1 m/s.
- A bare naive (no-tzinfo) timestamp does not crash.
- Campaign/session ids containing `..`, an absolute path, an embedded
  `/`, all rejected with `ValueError`; a symlinked `output_root` is
  followed correctly without permitting escape.
- Full velocity conversion succeeds with `socket.socket.connect`
  monkeypatched to raise - real proof of offline operation, not an
  assumption.

## SCIENCE contract validator

`reduce_engine/science_contract.py::validate_science_input` (new, pure,
read-only, opens only persisted MASTER SPECTRUM files - a monkeypatched
`open()` proves it never touches `data/mosaic`). Passes on every real
session produced in this pass. Rejects: missing manifest, unknown schema
version, a dangling point reference (manifest says COMPLETED, files
missing), and an injected `calibration_level="KELVIN"` (an absolute
claim REDUCE must never make).

## Output integrity / crash recovery / interrupt

- `reduce_engine/storage.py::validate_session` (new) - checks every
  expected file exists, point counts agree, no COMPLETED point is
  missing its H5/JSON pair. Passes on every real session.
- Simulated crash at point 3 of a 9-point campaign (monkeypatched
  `reduce_point` to raise): the exception propagates (no auto-resume
  magic that does not exist), the 2 already-completed points remain on
  disk as real evidence, and `manifest.json` is never written - so the
  session can never be mistaken for `COMPLETED`.
- Session-id collision: `ReduceSession.__init__` raises
  `FileExistsError` - confirmed both via direct unit test and via the
  real-historical test suite's own immutability check.

## Performance (large-ish real campaign)

See `docs/REDUCE_PIPELINE.md`'s new Performance section: 100 real points,
81.4s wall, 0.81s/point steady state, 333 MB peak RSS, 40.5 MB output
(~405 KB/point). `estimate_output_bytes()` predicts within 2.4% of this
real measurement. Top-3 bottleneck ranking reported, nothing rewritten
(measurement only, per instruction).

## Dependency audit

Non-stdlib imports across all of `reduce_engine/` and `almita_reduce.py`:
`astropy`, `h5py`, `numpy` (all pre-existing elsewhere in the repo) plus
4 local modules (`alignment_engine`, `calibration_foundation`,
`hi_spectral_metric`, `runtime_state` - all reused, not duplicated). Zero
new third-party dependencies added in either pass.

## Test counts

See `docs/REDUCE_V1_FREEZE.md`'s final numbers (filled in after the
complete suite's final run, including the real-historical files which
`pytest.skip()` cleanly when the local fixture campaigns are absent).
