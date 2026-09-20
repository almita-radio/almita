# SCIENCE V1 - FREEZE

Status: **FROZEN** at the commit that adds this file (`git log -1 -- docs/SCIENCE_V1_FREEZE.md`). Evidence:
`docs/SCIENCE_ACCEPTANCE.md` (296 SCIENCE tests, 3 real campaigns, byte-identical replays). Local commit only; not pushed.
Base: SCIENCE foundation `fd9ca3a514cd8586f1bc5dc291f00ae923a0014a`; REDUCE V1 frozen at `2afc4c5decf87a1c66562b4acf88e180bb127689`
(untouched).

## What is frozen
- **Science schema:** `science_schema_version = "1.0"`; manifest fields, product index entries and HDF5 self-description
  attributes as listed in `SCIENCE_MODEL.md` section 11; cube order `[velocity, y, x]`, velocity strictly ascending, x = east,
  y = north, row 0 = south; maps `[y, x]`.
- **Input REDUCE contract:** REDUCE schema "1.0" only; status FAILED refused; SCIENCE reads Level 1 only (never RAW, never
  `data/mosaic`); points sorted by `point_index`; exclusions always recorded with a reason.
- **Beam semantics:** operator-provided operational metadata (`gaussian_circular`, `beam_fwhm_deg`, `beam_source`, `beam_status`;
  V1 never emits `MEASURED`); mandatory explicit beam on the CLI; weight `exp(-4 ln2 theta^2/FWHM^2)`, zero beyond 3 x FWHM;
  pixel scale is sampling, not resolution.
- **Velocity resample semantics:** linear, explicit bracket/weight, GOOD only if every weighted source bin is GOOD and finite,
  no tolerance, no extrapolation, sigma interpolated linearly (per-channel conservative <= sqrt 2, integrated sigma correct);
  reference axis = lowest-`point_index` point, ascending.
- **Gridding equation:** `W = w_beam / sigma^2`; `y = sum(W v)/sum(W)`; `Var = sum(W^2 sigma^2)/(sum W)^2`; `n_pointings`, `weight_sum`.
- **Mask/NaN policy:** validity by selection before arithmetic; NaN/Inf never contributes; empty voxel = NaN/0/invalid; a valid 0 is a
  measurement; sigma floor `sqrt(2^20/finfo.max)`; local sigma consistency (fraction 0.1, window 129) excludes-and-counts.
- **Quality policy:** STRICT / STANDARD (default) / PERMISSIVE table; state vs `limitations`; product status
  VALID/PARTIAL/BLOCKED/FAILED; two status axes (`status`, `data_completeness`).
- **Uncertainty semantics:** propagated statistical sigma, independent-input model, beam-correlated pixels, no covariance.
- **Coverage semantics:** `n_pointings`, `weight_sum`, `spectral_coverage_fraction`; no effective integration time.
- **Integration:** bin-overlap over the actual channel bin edges, independent-channel sigma, coverage against the REQUESTED window,
  BLOCKED when the window misses the cube, PARTIAL_OUTSIDE_CUBE when it overlaps only partly.
- **Canonical products:** `cube/science_cube.h5`, `maps/integrated_relative_intensity.h5`, `maps/moment1_like_velocity_centroid.h5`,
  `maps/moment2_like_velocity_dispersion.h5`, plus `manifest.json`, `config.json`, `provenance.json`, `qc/{science_quality,
  resample_qc,coverage}.json`. PNG is not canonical.
- **Units:** relative_intensity dimensionless fractional excess; integrated `relative_intensity_dimensionless * m/s`; moments m/s;
  velocity LSRK m/s; angles degrees. No absolute calibration of any kind.
- **CLI:** `inspect`, `plan`, `run`, `replay`, `compare`, `validate`, `status`.

## Known limitations (unchanged by the freeze)
Provisional beam; no absolute calibration; statistical-only sigma (lower bound; real between-point scatter ~2.1x); no spatial
covariance; beam-smoothed gradients; narrow-line resample smoothing (peak loss up to 8 %, area conserved); approximate wide-field
grid footprint; no outlier rejection; no effective integration time; moment-like maps are experimental-grade.

## Deferred (not part of V1)
l-v diagram, FITS export, HI4PI comparison, cross-campaign stacking, bounding-box accumulation speed-up, float32 / compression.

## Freeze rule
After this commit the algorithm changes only for: a demonstrated bug, a new physical beam measurement, a new calibration
capability, field evidence, an explicit new science feature, or a schema version transition. No tuning of maps for appearance.
Future architecture item (REDUCE V2): carry explicit beam/grid metadata into Level 1.
