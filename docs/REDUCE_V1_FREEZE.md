# REDUCE V1: SCIENTIFICALLY ACCEPTED / CONTRACT FROZEN

Status: **FROZEN**, second pass, against `reduce_engine` as committed at
`9cf7dd7` plus the uncommitted second-pass work integrated by this
document's own commit. Evidence for every claim below lives in
`docs/REDUCE_ACCEPTANCE.md` (the full validation writeup) and the test
suite (`test_reduce_*.py`, 189 tests). This document is the frozen
contract itself, kept short by design (rule: no 5000-line rewrite).

## Scope (unchanged from `docs/REDUCE_SCOPE.md`)

`OBSERVE -> REDUCE -> SCIENCE (future)`. REDUCE turns LEVEL 0 (RAW,
immutable evidence) into LEVEL 1 (REDUCED SPECTRA). It never produces
spatial maps, cubes, or astrophysical interpretation - that is SCIENCE's
job, once SCIENCE exists. Full detail: `docs/REDUCE_SCOPE.md`.

## Schema version

`reduce_schema_version = "1.0"`. Versioning policy (`docs/REDUCE_MODEL.md`):
backward-compatible additions may land as `1.x`; anything that removes a
field, changes a unit, or changes an existing field's meaning requires
`2.0`. `reduce_engine.science_contract.validate_science_input` rejects an
unrecognized schema version outright rather than guessing - enforced by
`test_science_contract_rejects_unknown_schema_version`.

## Pipeline order (per point, `reduce_engine/pipeline.py::reduce_point`)

`ingest -> spectral_estimate -> mask -> calibration -> baseline -> rfi_ref
-> velocity -> resample -> average -> quality -> persist`. One capture
maps to exactly one point in every real campaign audited (`mosaic.csv`'s
`point_number` is unique per row); the campaign loop persists each point
independently and never aborts on one point's failure (`PointStatus.FAILED`,
never a crashed campaign).

## MasterSpectrum V1 contract

Full field table: `docs/REDUCE_MODEL.md`. Identity: `campaign_id`,
`point_index`, `reduce_session_id` (this pass's fix - previously absent,
now set by `ReduceSession.write_point()` and present both in the JSON
sidecar and as an HDF5 attr, so a `master_spectrum.h5` opened alone is
still traceable to its session; see "Self-description" below). Pointing:
`ra_hours`, `dec_degrees` (AZ/ALT and galactic l/b deliberately excluded
from V1 - see Known Limitations). Science arrays: `frequency_hz`,
`velocity_lsrk_m_s`/`velocity_frame`, `relative_intensity`, `uncertainty`,
`mask`, `n_contributing`. `integration_time_seconds`, `quality`
(`QualityReport`), `calibration_level`/`calibration_profile_id`/
`calibration_profile_hash`, `capture_refs[]` (provenance to RAW).

## Units (definitive, `docs/REDUCE_MODEL.md`)

`frequency_hz`: Hz, topocentric. `velocity_lsrk_m_s`: m/s (not km/s).
`ra_hours`: hours 0-24 (never degrees). `dec_degrees`: degrees -90..90.
`integration_time_seconds`: seconds. `relative_intensity`/`uncertainty`:
dimensionless fractional excess - never Kelvin/dBm/Jansky. `mask`: int64
`MaskFlag` bitmask. `n_contributing`: integer count.

## Quality states

`GOOD` / `WARNING` / `BAD` / `UNKNOWN` (`reduce_engine.models.QualityState`).
`reasons` is never empty, even for `GOOD`. Adversarial matrix
(`test_reduce_quality_adversarial_matrix.py`, 11 real/synthetic
scenarios): **FALSE-GOOD count: 0, FALSE-BAD count: 0** (see Test
Results below for the exact run that produced this number).

## Calibration levels supported

`RELATIVE` or `UNCALIBRATED` only - `MasterSpectrum.__post_init__` hard-
rejects anything else. An incompatible calibration profile (sample rate,
gain, or any other mismatch the compatibility model checks) is never
silently applied: `calibration_level` falls back to `UNCALIBRATED` with
an explicit reason in `quality.reasons` (`test_reduce_calibration_matrix.py`).
Future `ABSOLUTE` calibration contract (not implemented, no code path
exists): documented in `docs/REDUCE_MODEL.md`.

## Velocity frame supported

`lsrk` (`alignment_engine.hi.velocity.SUPPORTED_FRAMES`), or
`velocity_frame="UNAVAILABLE"` when required metadata is missing - never
a silently wrong value. Independently cross-checked against
`SkyCoord.radial_velocity_correction("barycentric")` (a different astropy
entry point) across 5 dates/positions; disagreement is of the expected
order of the Sun's own peculiar motion relative to the barycentric frame,
not error (`test_reduce_velocity_crosscheck_and_offline.py`).

## Self-description (section 42, this pass's fix)

`master_spectrum.h5` carries its own HDF5 attrs -
`reduce_schema_version`, `campaign_id`, `reduce_session_id`,
`point_index`, `calibration_level`, `velocity_frame`, `quality_state`,
and a `units_json` string covering every array - so the file is
identifiable and interpretable **in isolation**, without its JSON
sidecar, the session manifest, or any external doc. Verified by
`test_master_spectrum_h5_is_self_describing_without_its_json_sidecar` and
`test_master_spectrum_json_carries_its_own_session_id`
(`test_reduce_science_contract_and_integrity.py`).

## Offline guarantee

Zero network imports anywhere in `reduce_engine/` or `almita_reduce.py`
(grepped: no `requests`/`urllib`/`socket`/`http.client`). IERS
auto-download disabled (`auto_download=False`, `auto_max_age=None`,
confirmed set); a full velocity conversion succeeds with
`socket.socket.connect` monkeypatched to raise
(`test_reduce_velocity_crosscheck_and_offline.py`). No hardware imports
(no INDI, no `rtl_tcp`, no mount control) anywhere in REDUCE.

## SCIENCE-input guarantee

`reduce_engine/science_contract.py::validate_science_input` is pure,
read-only, and opens only persisted MASTER SPECTRUM files - a
monkeypatched `open()` proves it never touches `data/mosaic`
(`test_science_contract_never_opens_raw_mosaic_directory`). SCIENCE is
meant to consume only a REDUCE session's manifest, MASTER SPECTRA,
coordinates, masks, uncertainty, quality, and provenance - never raw IQ,
never a re-run FFT, never recalibration, never a redone Doppler
correction (`docs/REDUCE_SCOPE.md`).

## Test results (this freeze's own clean run, isolated - no concurrent suites)

| suite | count | result |
|---|---|---|
| Replay determinism - isolated single test | 1 | **PASS** |
| `test_reduce_replay_compare_provenance.py` (full file, incl. the same determinism test again) | 8 | **8 passed** |
| Fast batch (20 files: adversarial, averaging, baseline, calibration, cli, edge, golden, ingest, mask, models, quality-matrix, resample, rfi_ref, spectral, timezone/security, uncertainty, velocity) | 160 | **160 passed** |
| `test_reduce_multi_campaign_real.py` (3 real campaigns: 9/9, 100/100, 50/100 PARTIAL) | 4 | **4 passed** |
| `test_reduce_real_historical.py` | 5 | **5 passed** |
| **Total (full collection matches sum)** | **189** | **189 passed, 0 failed, 0 skipped, 0 error** |

Raw immutability: SHA-256 of every file under all 3 real campaign
directories, before and after REDUCE ran, identical
(`test_reduce_multi_campaign_real.py::test_real_campaign_matrix`).
Active-campaign safety: `ALMITA-OBSERVE-20260917-20:10:26` was confirmed
RUNNING during this pass (`quicklook_live.py`/`rtl_tcp` alive against it)
and was never opened, read, or processed.

**Note on the replay-determinism test**: an earlier run of this same test
FAILED while it was executing concurrently with another full REDUCE test
suite on this Pi (a second Claude Code session validating the same
commit independently). Re-run in isolation, with nothing else competing
for CPU/memory, it passed - twice, once alone and once as part of the
full 8-test file. Treated as a resource-contention artifact **only**
because that was independently re-confirmed clean, not assumed.

## Correction to `docs/REDUCE_ACCEPTANCE.md`

That document (written by the concurrent session) attributes a sigma-clip
"swamping" fix and an `equal_weight`/NaN-uncertainty fix to
`reduce_engine/averaging.py` in this pass. `git diff` against `9cf7dd7`
shows **zero changes** to that file - the robust median/MAD outlier
detector and the per-method `usable` mask it describes are already
exactly what's committed at `9cf7dd7`. Either that fix was already part
of the first pass, or the description is inaccurate; either way, no code
change exists to attribute to this pass. What's independently verified
instead: the code **currently at HEAD already behaves correctly** -
`test_reduce_averaging_realqa_and_stress.py` (8/8) and the robust-
stacking stress test both pass against the unmodified file.

## Known limitations (explicit, not hidden)

- **Absolute calibration unavailable.** `RELATIVE`/`UNCALIBRATED` only.
- **RFI_REF not associated per point.** OBSERVE's current output has no
  robust MAIN-point <-> RFI_REF-measurement linkage. Minimum future
  interface proposed, not implemented: `docs/REDUCE_RFI_REF_INTERFACE.md`.
  OBSERVE itself was not modified in this pass.
- **Beam effects, spatial gridding, and all SCIENCE-level products** are
  out of scope for REDUCE by design (`docs/REDUCE_SCOPE.md`).
- **Velocity unavailable if metadata is incomplete** - reports
  `velocity_frame="UNAVAILABLE"`, never a guessed value.
- **AZ/ALT and galactic l/b are not part of the V1 schema.** OBSERVE's
  `altitude_deg_at_goto`/`azimuth_deg_at_goto` are GOTO-time snapshots,
  not valid at the actual capture timestamp - carrying them into
  MasterSpectrum without recomputing would be a quiet inaccuracy, so V1
  omits them rather than store something misleading.
- **`stack_spectra` (the averaging primitive) has no mixed-configuration
  or duplicate-capture compatibility gate.** It would blindly average a
  wildly different-amplitude "capture" or double-count a literal
  duplicate if handed one. **Not reachable through the real V1 pipeline
  today** - `reduce_engine.ingest` maps exactly one capture to one point
  for every real campaign audited. Pinned by regression tests so a
  future multi-capture-per-point ingest path cannot silently reintroduce
  this unnoticed.
- **Uncertainty is not perfectly calibrated.** Monte Carlo coverage
  measured in [0.3, 3.0]x of nominal, not exactly 1x - a real, understood
  systematic (`hi_spectral_metric`'s median-combine PSD estimator is
  biased relative to a *theoretical* noise level; self-consistent and
  invisible in REDUCE's own relative outputs). Documented in
  `docs/REDUCE_ACCEPTANCE.md`; not something this pass "fixed" because
  it isn't wrong for what REDUCE actually reports.
- **No absolute-timing NTP/GPS cross-check** - REDUCE trusts OBSERVE's
  recorded UTC timestamps as-is.

## Freeze rule (per operator instruction)

REDUCE V1 is now FROZEN. After this point: no algorithm changes without
a real bug, a regression, field evidence, a new calibration capability,
or an explicit operator request. No tuning without data.
