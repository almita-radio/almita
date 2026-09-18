# HI sky alignment — model and scope (as of this pass)

## Question being answered

"How far off is ALMITA's real pointing from where the mount thinks it is
pointing?" — measured at night, using neutral hydrogen (21cm HI), when the
Sun is unavailable. Never a substitute for absolute flux calibration.

## Non-absolute-amplitude philosophy

`Observed_i = A * Reference_i(offset) + B + noise`. `A` (relative gain) and
`B` (baseline) are nuisance parameters the fit profiles out (existing
`fitting._profiled_fit`, reused unchanged) — a pointing answer must not
come from "we expected X Kelvin," because system gain/Tsys/bandpass are
not independently calibrated here.

## Reference trust

`alignment_engine/hi/reference_trust.py`: `SYNTHETIC` → `TEST_FIXTURE` →
`REAL_UNVERIFIED` → `REAL_VALIDATED`. Only `REAL_VALIDATED` can ever be
`SYNC`-eligible (`alignment_engine/hi/quality.py::evaluate_sync_eligibility`,
no override). Opening a FITS file successfully is never sufficient for
`REAL_VALIDATED` — see `fits_reference.validate_reference()`.

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
name raises `ValueError` — never a silent topocentric fallback.

## Spectral pipeline (`alignment_engine/hi/spectral_pipeline.py`)

Fixed order: mask invalid/edge/RFI channels (excluded, never zeroed) →
fit a low-order polynomial baseline outside the line window → subtract →
integrate the line window → propagate uncertainty from off-line channel
noise. Deliberately excludes FFT-peak detection and pre-mask smoothing.

## Target selection (`alignment_engine/hi/target_selection.py`)

Extends the existing `choose_hi_region()` (altitude + probe-pattern
contrast) with an explicit local-gradient check, so a region that is flat
exactly where the beam sits — even if a distant feature makes the wider
probe pattern noisy — is rejected, not preferred.

## Fit + quality

Reuses `alignment_engine/fitting.fit_raster()` unchanged (the same
sub-grid NLS refinement built for solar) — HI's `A`/`B` nuisance
parameters are exactly its existing profiled-fit machinery, not a new
fitter. `alignment_engine/hi/quality.py` adds a stricter, `ReferenceTrust`-
aware `ELIGIBLE`/`NOT_ELIGIBLE` gate on top, checking fit rating,
confidence, valid fraction, an absolute offset safety ceiling, and
distance from the raster edge.

## What is NOT built yet (explicit, not silently skipped)

- No real survey data. Recommendation: HI4PI (see the pass report), never
  auto-downloaded.
- `HI_SPECTRAL_PROFILE` (full line-shape/multi-component comparison) — the
  spectral pipeline extracts one scalar per pointing; the API does not
  close this off, but it is not implemented.
- `engine.py::run_hi_simulated()` still uses the older direct-scalar
  synthetic metric, not yet wired to the new spectral pipeline (a real
  spectrum-in, offset-out async path is the natural next step).
- HEALPix reference support (no `healpy` on this Pi) — the FITS provider
  supports CAR/TAN/SIN-projected cutouts only, which is exactly the format
  HI4PI's own small-cutout product ships in.
- Cross-validation across two independent targets, a persisted pointing
  model, elliptical/position-dependent beams, and full memory/performance
  characterization against a real large survey file.
