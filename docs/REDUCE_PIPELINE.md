# REDUCE: Pipeline

## Canonical stage order

```
INGEST -> VALIDATE -> SPECTRAL ESTIMATE -> MASK -> RELATIVE CALIBRATION ->
BASELINE -> VELOCITY FRAME -> RESAMPLE COMMON GRID -> AVERAGE/STACK ->
UNCERTAINTY -> QUALITY -> PERSIST LEVEL 1
```

Implemented in `reduce_engine/pipeline.py::reduce_point` (per point) and
`reduce_campaign` (per campaign). Each stage's output is explicit and
stateless; a point's failure at any stage is caught, recorded with a
reason, and the campaign continues (see "Failure semantics" below).

### INGEST (`reduce_engine/ingest.py`)

Discovers campaign structure from `mosaic.csv` + `grid_metadata.json` +
`observer_config.json` - never hardcodes a campaign id or point count.
Matches each row's declared `data_filename` to a real file **by stem**,
not by extension: real capture metadata records a `.dat` extension while
`sdr_capture.py` actually writes `.h5` (a real, kept discrepancy - see
`test_reduce_ingest_and_resample.py`). Missing metadata resolves to
`"UNKNOWN"` (`ingest.metadata_field`), never a guessed default.

### VALIDATE (`reduce_engine/validation.py`)

Offline-only preflight gates: campaign readable, at least one accepted
point, calibration profile readable (if requested), `fft_size` valid,
velocity-frame metadata available (if a non-topocentric frame was
requested), output destination writable. No hardware gate exists because
none is needed - REDUCE never touches hardware.

### SPECTRAL ESTIMATE (`reduce_engine/spectral.py`)

Reuses `hi_spectral_metric.robust_psd_from_iq` verbatim: raw `uint8`
interleaved IQ -> centered at `127.5` -> complex -> Hann-windowed FFT ->
median-combined `|FFT|^2` across sub-integration segments. Same capture +
same `fft_size`/`combine` -> same result (determinism). Registers
`fft_size`, `window`, `combine`, `sample_rate_hz`, `center_frequency_hz`,
`iq_center`, `n_segments`.

### MASK (`reduce_engine/masks.py`)

Builds the multi-reason `MaskFlag` bitmask:

- **DC**: `source="calibration_profile"` (reused evidence) when a
  compatible profile is available, else `"measured_per_capture"` via
  `hi_spectral_metric.measure_dc_mask_half_width` on this capture's own
  spectrum.
- **KNOWN_SPUR**: same preference - profile evidence first, else
  `hi_spectral_metric.detect_fixed_spurs` across this capture's own FFT
  sub-integration segments.
- **EDGE**: `source="configured_default"` (`ReduceConfig.edge_fraction`,
  2% each side by default) - no calibration-derived edge-rolloff evidence
  was found in the audited codebase for this pass, so the conservative
  configured default is used and explicitly labeled as such.
- **INVALID**: non-finite or non-positive PSD values.
- **SATURATED**: set for every bin in a capture whose ADC clipping
  fraction exceeds tolerance - clipping is a whole-capture condition, not
  a per-frequency-bin one.
- **RFI**: set only by the RFI_REF stage (see below) - never overlaps
  with KNOWN_SPUR's within-capture detection.

### RELATIVE CALIBRATION (`reduce_engine/calibration.py`)

Consumes `calibration_foundation.py`'s existing profile format verbatim
(`load_calibration_profile`, `check_calibration_compatibility`,
`apply_relative_calibration_to_psd`) - never reimplements the reference
ensemble or the correction formula. `calibration_level` is `"RELATIVE"`
only when a profile was supplied AND compatibility returned
`"COMPATIBLE"`; otherwise `"UNCALIBRATED"`, with the specific reason
(`NOT_ATTEMPTED` / `INCOMPATIBLE` / `UNKNOWN`) recorded. Never applies a
profile to an incompatible capture, even partially.

**Audited discrepancy, documented not silently resolved:**
`calibration_engine/profile.py`'s `CalibrationProfile` is a *different*,
DRAFT-only, human-review-gated operational-envelope descriptor
(recommended gain, known masks) using `CalibrationLevel.OPERATIONAL_RELATIVE`;
it has no PSD-array apply function. REDUCE consumes
`calibration_foundation.py`'s numeric profile (`RELATIVE_INSTRUMENTAL`
internally) because that is the one that can actually be *applied* to a
spectrum. Both describe "relative, not absolute" calibration; REDUCE's
own `calibration_level` field is always `RELATIVE` or `UNCALIBRATED`,
independent of either subsystem's internal naming.

### BASELINE (`reduce_engine/baseline.py`)

Reuses `hi_spectral_metric.fit_polynomial_baseline` verbatim (robust,
iteratively-sigma-clipped low-order polynomial, default degree 2). The
fit region always excludes every non-`GOOD` bin AND the HI line window
(`|v_radio| <= 150 km/s` by default) - the line is never used to fit its
own baseline. Persists `baseline_model`, `baseline_parameters`,
`fit_mask` (bins the robust fit actually used after its own internal
clipping), and `fit_quality_rms_fraction`.

### VELOCITY FRAME (`reduce_engine/velocity.py`)

Reuses `alignment_engine.hi.velocity` verbatim (astropy-backed,
`SUPPORTED_FRAMES = {topocentric, heliocentric, barycentric, lsrk}`,
fail-safe `ValueError` on an unsupported frame name - never a silent
topocentric fallback). Requires observation time + observer location +
sky direction; any missing field reports `velocity_frame="UNAVAILABLE"`
with the specific missing fields named, never an invented correction.

**Performance note (found for real, fixed in this pass):** astropy's
default IERS behavior can (a) attempt a network download of
Earth-orientation bulletins, and (b) raise once its bundled table is more
than 30 days old relative to wall-clock time - both fatal to REDUCE's
"100% offline, always works" contract on a machine that has been offline
for a while. `reduce_engine/velocity.py` sets
`astropy.utils.iers.conf.auto_download = False` and
`auto_max_age = None` at import time. Real 9-point campaign timing: >200s
(intermittent hang/error) before this fix, single-digit seconds of
velocity-stage time after it.

**Per-bin cost note:** the frame shift (topocentric -> LSRK/etc) is
computed **once** per point via `alignment_engine.hi.velocity.frame_shift_km_s`
and then added as a constant offset to the whole (vectorized) topocentric
velocity axis - not re-invoked per frequency bin. This is exact, not an
approximation: the IAU radio-velocity convention is affine in frequency,
and a frame change is a single additive velocity offset independent of
frequency (the same fact `frame_shift_km_s` itself documents and relies on).

### RESAMPLE TO COMMON GRID (`reduce_engine/resample.py`)

A **no-op** whenever every input spectrum already shares the same
frequency axis (the common case within one campaign, where every capture
used the same `fft_size`/`sample_rate_hz`/`center_frequency_hz`) - per
ALMITA's existing NATIVE_GRID contract (never interpolate when a native
common grid already exists). Interpolation only runs to reconcile
genuinely different grids (e.g. combining sessions with different sample
rates), is always mask-aware (a resampled bin is `GOOD` only if both
source bins it came from were `GOOD`), and never converts a masked gap
into fabricated data.

### AVERAGE/STACK (`reduce_engine/averaging.py`)

The one central `stack_spectra` function - never a naive `np.mean()`.
Default: `inverse_variance_weighted` with an explicit, registered
fallback to `equal_weight` when no valid per-bin uncertainty exists for a
given method/scenario (never silently substituting a fabricated weight).
Outlier rejection during stacking uses a **robust median/MAD** center and
spread per bin (not the weighted mean/variance being solved for) - a
single extreme pathological spectrum otherwise inflates its own detection
threshold ("swamping") and survives every clipping iteration undetected;
this was found and fixed during this pass's own testing
(`test_reduce_averaging.py::test_sigma_clipping_removes_one_pathological_spectrum`).

**Evidence for the default** (`test_reduce_averaging.py`'s parametrized
robustness comparison, Monte Carlo over Gaussian noise, a single RFI
spike, gain drift, missing bins, and one pathological spectrum): the
default method is never worse than a naive per-bin mean, and is at least
2x better in RMS-to-true-value whenever a scenario contains a genuine
outlier or corrupted region.

`integration_gain_curve()` computes the `1/sqrt(N)` reference curve as an
explicit **diagnostic only** - real behavior (correlated noise, RFI,
drift) may deviate, and REDUCE never asserts `1/sqrt(N)` as a guaranteed
law.

### UNCERTAINTY (`reduce_engine/uncertainty.py`)

Per-capture: a robust (MAD-based) standard error of the median-combined
PSD, per bin, from that capture's own independent FFT sub-integration
segments (`per_capture_uncertainty`) - never a single global scalar
broadcast to every bin. After stacking: `AverageResult.uncertainty` is
either the analytic inverse-variance combination (`1/sqrt(sum(weights))`)
or the empirical dispersion-based standard error, per the weighting
actually used. Global QC (`uncertainty.global_metrics`):
`usable_fraction`, `median_noise`, `effective_integration_time_seconds`.

### QUALITY (`reduce_engine/quality.py`)

Combines: ADC clipping fraction, usable fraction, RFI occupancy, baseline
fit RMS, calibration compatibility, contributing-spectra count, and
velocity-frame availability into one `QualityReport`. `BAD` if clipping
detected, usable fraction too low, no contributing spectra reach a bin,
or the baseline fit RMS is too high. `WARNING` (given no `BAD` trigger)
if uncalibrated, RFI occupancy is high, or the velocity frame is
unavailable. `GOOD` otherwise - and even then, `reasons` is never empty
(it states the metrics that justified GOOD).

### RFI_REF (`reduce_engine/rfi_ref.py`)

V1 use is **veto/flag only** - never adaptive cancellation or
subtraction. A bin is flagged `RFI` only when MAIN shows a statistically
significant excess over its own baseline **and** RFI_REF shows a
coincident significant excess at the same frequency, within
`rfi_ref_max_time_delta_seconds`. "Any common signal" is deliberately
**not** the criterion - that would flag genuine sky structure seen by
both antennas. Registers `RFI_REF_AVAILABLE`, `RFI_REF_USED`,
`RFI_REF_TIME_DELTA`, `RFI_REF_MATCH_CONFIDENCE` (as
`RfiRefOutcome.available/used/time_delta_seconds/match_confidence`).
Continues without RFI_REF whenever it is unavailable - never blocks the
pipeline.

## Failure semantics

- `PointStatus.COMPLETED` / `BLOCKED` (pre-existing reason: rejected at
  ingest) / `FAILED` (an exception during processing) - a point never
  silently disappears.
- `CampaignReduceReport.status`: `COMPLETED` (all accepted points
  completed), `PARTIAL` (some completed, some blocked/failed), `FAILED`
  (zero points completed). A campaign is **never** reported `COMPLETED`
  while silently missing data.

## Determinism & provenance (`reduce_engine/provenance.py`, `config.py`)

`ReduceConfig.config_hash()` (SHA-256 over the sorted JSON of every
config field) + git commit + calibration profile hash + per-input
`sha256` + software versions (`python`, `numpy`, `h5py`, `astropy`) +
`random_seed` + UTC start/end + hostname, written to every session's
`provenance.json`. Same input + same config + same software -> same
output (see `test_reduce_real_historical.py::test_replay_reprocesses_from_raw_into_a_new_session`,
which asserts an exact-zero RMS difference between an original run and
its replay).

## Storage (`reduce_engine/storage.py`)

```
data/reduced/CAMPAIGN_ID/REDUCE_SESSION_ID/
    manifest.json       # session-level status, counts, per-point outcomes
    config.json          # the ReduceConfig used
    provenance.json       # see above
    points/<point_index>/master_spectrum.{h5,json}
    qc/
    logs/{session.log,events.jsonl}
```

Arrays in HDF5, metadata/provenance in JSON - never pickle. A
`ReduceSession` directory is created once and raises `FileExistsError` on
any attempt to reuse a `REDUCE_SESSION_ID` - reprocessing always gets a
new session id (`storage.new_reduce_session_id`, microsecond-resolution,
matching `calibration_engine/session.py`'s own precedent and its stated
reason: two runs completing within the same wall-clock second must never
collide).

## Replay & compare

`reduce_engine/replay.py::replay_session` reads a previous session's
`manifest.json` for its `source_campaign_root`, re-discovers that
campaign fresh, and re-runs the full pipeline into a **new** session -
100% offline, the raw campaign is the only thing it reads from outside
the REDUCE tree. `reduce_engine/compare.py::compare_sessions` is a
regression-oriented comparator only (same source? same config? same
point count? per-point RMS difference) - no astrophysical comparison.
