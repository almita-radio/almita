# SCIENCE: Data Model (V1)

`science_engine.models.SCIENCE_SCHEMA_VERSION = "1.0"`. Independent of
`reduce_engine.models.REDUCE_SCHEMA_VERSION` - SCIENCE's schema versions
its own output, never conflated with REDUCE's (section 55).

## ScienceInput / ScienceInputPoint

The result of `science_engine.ingest.load_science_input()` - the ONLY
way data enters SCIENCE. Gated by `reduce_engine.science_contract.
validate_science_input` first; raises `ScienceContractError` before
touching a single array if that gate fails. `ra_deg` is computed once,
explicitly, from the source `ra_hours` at ingest (`ra_hours * 15.0`) -
downstream code only ever sees degrees, never hours (section 12).

## BeamModel

| field | meaning |
|---|---|
| `model_type` | always `"gaussian_circular"` in V1 |
| `fwhm_deg` | full width at half maximum, degrees |
| `source` | free text - where this FWHM actually came from (never omitted) |
| `status` | `CONFIGURED_OPERATIONAL` \| `PROVISIONAL_OPERATIONAL` \| `MEASURED` (V1 never produces `MEASURED`) |
| `cutoff_n_fwhm` | beam response is defined to be exactly 0 beyond this many FWHMs (default 3.0 - see rationale in `science_engine/beam.py`) |

Formula: `w(theta) = exp(-4 ln(2) * theta^2 / FWHM^2)`. `theta=0 -> 1`,
`theta=FWHM/2 -> 0.5` exactly (the definition of FWHM) - verified in
`test_science_beam.py`, not just asserted.

See `docs/SCIENCE_SCOPE.md`'s "Beam FWHM: a documented gap" section for
where the default value actually comes from and why V1 does not
auto-discover a per-campaign value.

## ScienceGrid

A local tangent-plane grid (`frame="icrs"` always the canonical storage
frame; Galactic l/b is a derived view via `science_engine.spatial.
to_galactic`, never a second storage frame). Same projection convention
as OBSERVE's own `grid_generator.py` (`"tangent-plane"`, RA scaled by
`cos(dec)`), sized to the real input points' extent plus a beam-derived
margin (`config.extent_margin_beams * beam.fwhm_deg`) - never a
hardcoded shape. `pixel_scale_deg` defaults to `beam.fwhm_deg /
config.pixels_per_beam` (default 4 px/beam) - fine enough for a smooth
plot without ever claiming spatial resolution finer than the beam
(section 11): the beam FWHM, not the pixel count, is always the honest
effective resolution, and every persisted map/cube carries both.

## ScienceCube

Axis order is always **[velocity, y, x]** - matches
`alignment_engine/hi/cube_reduction.py`'s own `(vel, lat, lon)`
convention (this repo's other, independent cube producer), pinned by
`test_science_cube.py::test_cube_axis_order_is_velocity_y_x` so a future
change can never silently transpose axes (section 126).

| array | shape | meaning |
|---|---|---|
| `velocity_lsrk_m_s` | `(Nv,)` | canonical cube velocity axis - see "Velocity axis resampling" below |
| `relative_intensity` | `(Nv,Ny,Nx)` | beam+inverse-variance-weighted mean, NaN where no valid contribution |
| `uncertainty` | `(Nv,Ny,Nx)` | propagated per the GENERAL weighted-mean formula (see below) |
| `weight_sum` | `(Nv,Ny,Nx)` | sum of applied weight - a QC/edge-effect diagnostic |
| `n_contributing` | `(Nv,Ny,Nx)` | integer count of input points that contributed |
| `valid` | `(Nv,Ny,Nx)` bool | `weight_sum > 0` |

### Uncertainty propagation (section 32) - the pitfall this pass caught

The per-voxel estimator is a weighted mean `y = sum(w_i x_i)/sum(w_i)`
with `w_i = beam_i / sigma_i^2` (beam AND inverse-variance weighting
combined). Its correctly propagated variance is the GENERAL weighted-mean
formula:

```
Var(y) = sum(w_i^2 * sigma_i^2) / (sum(w_i))^2
```

**not** the pure-inverse-variance shortcut `1/sqrt(sum(w))`, which only
holds when every `w_i` IS exactly `1/sigma_i^2` with no other factor.
The first draft of `GriddingAccumulator` in this pass used the wrong
shortcut; caught by a direct brute-force numerical check before any
other module was built on top of it
(`test_science_gridding.py::test_2d_map_accumulator_matches_brute_force`).

### Velocity axis resampling (real-data finding)

REDUCE guarantees a common `frequency_hz` grid within a campaign but
NOT a common `velocity_lsrk_m_s` grid - confirmed on real data (up to
641 m/s / 10.4 channels of real, physically-expected direction+time-
dependent disagreement across a 9-point campaign; see
`docs/SCIENCE_SCOPE.md`). `science_engine.cube.build_cube` therefore
resamples each point's spectrum onto ONE canonical velocity axis (the
first accepted point's own axis) via `science_engine.resample`
whenever a point's own axis doesn't already match within tolerance -
linear interpolation of an ALREADY-Doppler-corrected axis, mirroring
REDUCE's own frozen `resample.py`'s exact mask-aware convention
(a resampled bin is GOOD only if interpolation drew, to numerical
certainty, only from GOOD source bins). This is not "SCIENCE
recalculates Doppler" - no frequency-to-velocity physics happens here.

### The NaN-poisoning bug this pass found and fixed

A masked bin's `relative_intensity` is NaN in real REDUCE data, always
(confirmed: 343/343 masked bins on a real point, 0 GOOD bins). `w_i * NaN
== NaN` even when `w_i == 0` (IEEE754) - so a masked bin's correctly-zero
weight did NOT stop its NaN value from poisoning
`GriddingAccumulator`'s running sum via `+=`. The entire first real run
of this pass produced an all-NaN integrated map because of this.
Fixed: the VALUE (not just the weight) is now explicitly zeroed with
`np.where(bin_valid, value, 0.0)` before the weighted multiply -
`science_engine/gridding.py`'s `add_point`. `science_engine/simulation.py`
was also updated to inject real NaN at masked synthetic positions
(it previously didn't, which is exactly why the original synthetic test
suite never caught this) and a direct regression test
(`test_nan_valued_masked_bin_does_not_poison_the_running_sum`) pins it.

## SpatialMap

A generic 2D `(Ny,Nx)` product - `kind` distinguishes
`integrated_relative_intensity`, `moment1_like_velocity_centroid`,
`moment2_like_velocity_dispersion`, and `channel_map`. Always carries
`value`, `weight_sum`, `n_contributing`, `valid`, and `units` - an
invalid pixel is always NaN in `value`, never a fabricated 0 (section 27/67).

## Units (definitive)

| field | unit | notes |
|---|---|---|
| `velocity_lsrk_m_s` | m/s | matches REDUCE's own canonical unit |
| `ra_deg`/`dec_deg`/grid coordinates | degrees | canonical storage frame is ICRS |
| `relative_intensity` | dimensionless fractional excess | never Kelvin/Jy/dBm - inherited exactly from REDUCE |
| integrated map value | `relative_intensity_dimensionless * m/s` | never called "K km/s" (section 38) |
| `moment1_like` | m/s | velocity centroid |
| `moment2_like` | m/s | velocity dispersion (a standard deviation, not a variance) |
| `beam.fwhm_deg`, `pixel_scale_deg` | degrees | |

## SCIENCE quality states

Same 4-state vocabulary as REDUCE (`GOOD`/`WARNING`/`BAD`/`UNKNOWN`) but
a **separate layer** - never overwrites or is overwritten by an input
point's own REDUCE quality (section 131). Reasons include
`INSUFFICIENT_POINTS`, `LOW_SPATIAL_COVERAGE`, `INPUT_WARNINGS`,
`NO_VELOCITY` (BAD), `BEAM_MODEL_PROVISIONAL` (always present in V1,
since V1 never produces a `MEASURED` beam), and an UNCALIBRATED-input
warning. `reasons` is never empty, even for GOOD
(`science_engine/quality.py`).

## HDF5 vs a future FITS export

HDF5 (`science_cube.h5`, one `.h5` per map under `maps/`) is V1's only
canonical persisted format - self-describing via HDF5 attrs
(`science_schema_version`, `campaign_id`, `reduce_session_id`,
`science_session_id`, `axis_order`, `units_json`/`units`,
`grid_json`, `metadata_json`), openable in isolation without any
external doc, mirroring REDUCE's own `master_spectrum.h5` self-
description precedent. No FITS export exists in V1 (see
`docs/SCIENCE_SCOPE.md`); if added later, HDF5 stays the canonical
internal product and FITS becomes an interoperability/export-only
format, with a real `astropy.wcs.WCS` and an honest `BUNIT` (never `K`
for a dimensionless relative product) - never a hand-built fake header.
