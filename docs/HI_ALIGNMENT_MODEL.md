# HI sky alignment — model and scope

## Question being answered

"How far off is ALMITA's real pointing from where the mount thinks it is
pointing?" — measured at night, using neutral hydrogen (21cm HI), when the
Sun is unavailable. Never a substitute for absolute flux calibration.
FIRST_LIGHT_HI phase: this answers "do we see the predicted pattern", not
"do we trust it enough to correct the mount" — SYNC stays blocked
regardless of how good a single result looks (`hi/sync_policy.py`).

## Current deployment status (see project memory)

As of this writing the ALMITA mount/antenna are set up **indoors**, not
field-deployed pointing at open sky. Everything in this document and
package is validated in simulation and against real downloaded reference
data; no real GOTO/mount-movement HI observation has been executed yet -
`hw_hi_night_scan.py` is built, tested, and rehearsed, waiting on field
deployment, not on remaining software work.

## Non-absolute-amplitude philosophy

`Observed_i = A * Reference_i(offset) + B + noise`. `A` (relative gain) and
`B` (baseline) are nuisance parameters the fit profiles out (existing
`fitting._profiled_fit`, reused unchanged) — a pointing answer must not
come from "we expected X Kelvin," because system gain/Tsys/bandpass are
not independently calibrated here.

## Reference data — real, downloaded, validated

HI4PI (CDS/VizieR J/A+A/594/A116), one 20x20deg Galactic CAR-projection
cube (`CUBES/GAL/CAR/CAR_E01.fits`, GLON=10 GLAT=0, 264067200 bytes,
933-channel LSRK VRAD, BUNIT=K, ~16.24 arcmin beam) fetched via legacy FTP
(the HTTPS CDS gateway sits behind a JS bot-challenge). Stored under
`data/reference/hi/hi4pi/` — **not committed to git** (253 MB, this
repo's no-gigabytes-in-git convention) — with a manifest alongside.
Reduced to a 2D moment-0 map (`alignment_engine/hi/cube_reduction.py`,
+-100 km/s window, empirically confirmed to capture 95.6% of the real
integrated signal) and promoted `REAL_UNVERIFIED` -> `REAL_VALIDATED` via
9 independent objective checks (`cube_reduction.promote_to_validated()` -
checksum, WCS, spectral axis/frame, units, plausibility, a WCS
pixel-roundtrip spot-check; no `--force` anywhere).

`alignment_engine/hi/reference_trust.py`: `SYNTHETIC` -> `TEST_FIXTURE` ->
`REAL_UNVERIFIED` -> `REAL_VALIDATED`. Only `REAL_VALIDATED` can ever be
`SYNC`-eligible (`hi/quality.py::evaluate_sync_eligibility`, no override),
and even then FIRST_LIGHT_HI still blocks it unconditionally (see above).

## Coordinate frames

HI surveys publish in Galactic (l, b). ALMITA points in ICRS/EOD. All
conversions go through astropy directly (`SkyCoord(...).galactic` /
`.icrs`), never hand-rolled. Tangent-plane offset geometry for HI targets
has never been exposed to the solar `.icrs`-on-a-real-distance bug (see
the solar-fix commit) because catalog/FITS-pixel coordinates are built
without a physical distance in the first place.

## Spectral frame

ALMITA measures **topocentric** frequency. Surveys typically publish
**LSRK**. `alignment_engine/hi/velocity.py` converts via astropy's
`SpectralCoord.with_observer_stationary_relative_to(...)` — cross-checked
against the independent, dedicated `SkyCoord.radial_velocity_correction()`
API to within ~1 m/s for heliocentric/barycentric. An unsupported frame
name raises `ValueError` — never a silent topocentric fallback. (A first
draft mapped "barycentric" to the wrong astropy frame, GCRS instead of
ICRS - caught by that same cross-check before it reached any test.)

## Spectral pipeline (`alignment_engine/hi/spectral_pipeline.py`)

Fixed order: mask invalid/edge/RFI channels (excluded, never zeroed) →
fit a low-order polynomial baseline outside the line window → subtract →
integrate the line window → propagate uncertainty from off-line channel
noise. Deliberately excludes FFT-peak detection and pre-mask smoothing.
Now the ONE path both `engine.py::run_hi_simulated()` and the real night-
scan harness use (`alignment_engine/hi/acquisition.py::acquire_and_reduce_point`)
— no parallel HI-metric implementation.

## Acquisition (`alignment_engine/hi/acquisition.py`)

`HIAcquisitionBackend` protocol: simulated (spectral_simulation-driven)
and real (`RealHIAcquisitionBackend`, wraps `sdr_capture.SDRCapture` - the
same class `capture.py` itself uses, never a duplicated rtl_tcp client -
plus a small new FFT helper mirroring `sun_detectability_test.py`'s own
real-hardware-tested windowing pattern, since no existing function in this
repo returns a raw (frequency, power) array).

## Target selection (`alignment_engine/hi/target_selection.py` +
`fits_reference.FITSMomentMapProvider`)

Extends the existing `choose_hi_region()` (altitude + probe-pattern
contrast) with an explicit local-gradient check, so a region that is flat
exactly where the beam sits — even if a distant feature makes the wider
probe pattern noisy — is rejected, not preferred. Scoring uses the REAL
beam-convolved map (a bug where target *selection* silently used raw,
non-convolved pixels — while only the fitter's template was convolved —
was found and fixed while testing against the real cube).

Performance: `template_for()` builds ONE cached tangent-plane
interpolation grid (`alignment.LocalSphericalTemplate`, the same class the
synthetic catalog path already used) instead of an exact convolution per
evaluation — ~50x faster per fit on the real map (0.31s vs 16.8s for a
25-point raster), which is what makes the mandatory bootstrap (below)
tractable at all.

## Fit + quality

Reuses `alignment_engine/fitting.fit_raster()` unchanged (the same
sub-grid NLS refinement built for solar) — HI's `A`/`B` nuisance
parameters are exactly its existing profiled-fit machinery, not a new
fitter.

Two quality layers, deliberately separate from the fit itself (Fit Result
vs Quality Decision):
- `hi/quality.py`: `ReferenceTrust`-aware `ELIGIBLE`/`NOT_ELIGIBLE` gate
  (fit rating, confidence, valid fraction, absolute offset ceiling, raster-
  edge margin).
- `hi/quality_v2.py`: spatial coverage (valid count/fraction, row/column
  diversity, bounding-box coverage), a null-model improvement (`1 -
  residual^2`, already implicit in the existing fit, made explicit), edge
  proximity, and **mandatory bootstrap** (resample + refit via the SAME
  `fit_raster()`). Found by large-N Monte Carlo, not assumed: coverage +
  null-model checks ALONE can be *fooled* by a profiled A/B fit
  overfitting noise (worse false-GOOD rate than the original rating in
  some regimes) — bootstrap is what actually closes the two known
  false-GOOD gaps (extreme missing data, extreme noise). No `GOOD`
  verdict is reachable without it.

## Sync policy (`alignment_engine/hi/sync_policy.py`)

`AlignmentPhase.FIRST_LIGHT_HI` unconditionally blocks SYNC — no
parameter anywhere can override it — regardless of trust or quality.
`AlignmentPhase.ESTABLISHED_HI` additionally requires
`check_repeatability()`: at least 2 independent solutions (different
scans and/or targets) agreeing within a configurable sigma, before
`evaluate_phase_gate()` can ever return `sync_allowed=True`.

## First real HI night scan harness (`hw_hi_night_scan.py`)

Three preflight stages (system / astronomical / reference-structure),
each able to independently BLOCK before any mount write. Target is
recomputed from the persisted reference + config at every run, never
hardcoded; the reference is re-validated (checksum + structure) at the
start of every run. Rehearsed via `--backend simulated` (full pass path,
a real Ctrl-C-equivalent mid-scan cancellation, and a 100%-GOTO-failure
case) before any real invocation was even considered. Built and tested;
not yet run against real hardware — see "Current deployment status" above.

## What is NOT built yet (explicit, not silently skipped)

- `HI_SPECTRAL_PROFILE` (full line-shape/multi-component comparison) — the
  spectral pipeline extracts one scalar per pointing; the API does not
  close this off, but it is not implemented.
- HEALPix reference support (no `healpy` on this Pi) — the FITS provider
  supports CAR/TAN/SIN-projected cutouts only, which is exactly the format
  HI4PI's own small-cutout product ships in.
- Visual map products (PNG) for expected/observed/model/residual — JSON
  grids are persisted per session; rendering them as images is a follow-up.
- Cross-validation across two independent targets, a persisted pointing
  model, elliptical/position-dependent beams, and full memory/performance
  characterization against a real large survey file.
- Real hardware execution of `hw_hi_night_scan.py` — blocked on field
  deployment, not on remaining software readiness.
