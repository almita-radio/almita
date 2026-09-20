# SCIENCE: Data & Science Model (V1, FROZEN - see SCIENCE_V1_FREEZE.md)

`science_engine.models.SCIENCE_SCHEMA_VERSION = "1.0"`, independent of REDUCE's schema version. SCIENCE V1 supports
**REDUCE schema "1.0" only**: any other version, a REDUCE session with status `FAILED`, or a session with no usable
point is refused (fail closed, never guessed). Measured numbers quoted here come from `docs/SCIENCE_ACCEPTANCE.md`.

## 1. Input contract (the only door into SCIENCE)

`science_engine.ingest.load_science_input()` reads a REDUCE session directory (manifest + per-point
`master_spectrum.{json,h5}`) after `reduce_engine.science_contract.validate_science_input`. It never opens RAW, IQ,
`data/mosaic`, OBSERVE/CALIBRATE/ALIGN state or hardware (tested with an audit hook on a copy of a real session run from an
unrelated cwd, and by an import-boundary test: only `reduce_engine.models` and `reduce_engine.science_contract` are imported
from REDUCE, no spectral/baseline/Doppler code).

- Points are sorted by `point_index` (never manifest/filesystem order) -> deterministic summation order.
- A manifest point listed twice is refused. A point whose JSON `point_index` differs from the manifest is excluded.
- Points that do not become input are **never silently dropped**: `ScienceInput.exclusions` lists each with a reason
  (`REDUCE_POINT_STATUS_<status>`, `MISSING_COORDINATES`, `INVALID_COORDINATES`, `VELOCITY_FRAME_NOT_LSRK`, `MALFORMED_*`,
  `DUPLICATE_CAPTURE_OF_POINT_k`). A second point that references the same RAW capture sha256 is a duplicate measurement
  and is excluded, so it cannot fake integration time or lower uncertainty.
- Input identity: sha256 of each point's `master_spectrum.h5` and `.json` (~0.35 MB each, negligible cost), the sha256 of
  the REDUCE `manifest.json`, and the point's `capture_refs` (copied, RAW never opened) are stored in provenance.

## 2. Beam contract (operator-provided operational metadata)

REDUCE V1 carries no beam/grid metadata. The beam FWHM is **operator-provided operational metadata**. It is not a
characterised beam, and the observation grid spacing, the configured beam assumption and a physical beam measurement are three
different things. Persisted in every cube/map (HDF5 attrs) and the manifest: `beam_model` (`gaussian_circular`),
`beam_fwhm_deg`, `beam_source`, `beam_status` (`PROVISIONAL_DEFAULT` | `CONFIGURED_OPERATIONAL` | `MEASURED`; V1 never emits
`MEASURED`), plus `source_path`/`source_field`/`source_sha256` when read from a file.

- CLI: `--beam-fwhm-deg X` -> `source=operator_config`; or `--beam-from-observer-config [PATH]` -> reads
  `observation_defaults.beam_fwhm_deg` of the file the operator named and records path, field and sha256. With neither, the
  CLI refuses (no silent default). The library default (20 deg) is a labelled placeholder (`PROVISIONAL_DEFAULT`), not read from
  any file. SCIENCE never searches for a beam (never in `data/mosaic`).
- Valid range `[1e-3, 180]` deg, finite (0, negative, NaN, Inf, >180, absurdly tiny all raise).
- Weight: `w_beam(theta) = exp(-4 ln2 theta^2 / FWHM^2)`, FWHM = full width at half maximum of the RESPONSE in angle:
  `w(0)=1`, `w(FWHM/2)=0.5`, `w(FWHM)=0.0625` (tested). Exactly 0 beyond `beam_cutoff_n_fwhm` (default 3) x FWHM (weight there
  `2^-36 ~ 1.4e-11`; measured on real data: 2x cutoff changes the integrated flux by 0.29 %, 1x by 42 %, so 3x is kept).
- `pixels_per_beam`/`pixel_scale_deg` set output **sampling**, never resolution. Coarse pixel centres that coincide with fine
  ones carry identical values (tested).

Future improvement (not in V1, REDUCE stays frozen): OBSERVE/REDUCE could carry explicit beam metadata into Level 1
(a schema V2).

## 3. Geometry and axes

- Canonical frame ICRS. RA hours -> degrees once at ingest. Separations are great-circle (checked against an independent
  haversine and astropy at RA wrap, dec 89.99, 1e-3 deg separations, to 1e-9 deg). Galactic is a derived view only.
- Grid: local tangent-plane-like grid (RA offset x cos(dec_centre), RA wrap handled by the vector-mean centre and signed
  shortest RA difference). Pixel centres map exactly to sky positions, so weights are exact; the pixel footprint is only
  approximate for wide fields (see limitations).
- **Cube order `[velocity, y, x]`**; x increases with RA offset (east), y increases with Dec (north), row 0 is the southernmost
  row (`origin='lower'` for display). Maps are `[y, x]`. Pinned by tests with all three lengths different.
- **Velocity axis: strictly ascending in the cube, always** (real REDUCE Level 1 is descending). HDF5 attr
  `velocity_axis_order="ascending"`. A pure reversal is a permutation, not an interpolation.

## 4. Velocity resampling (real LSRK geometry, not a REDUCE bug)

REDUCE guarantees a common frequency grid; `velocity_lsrk_m_s` differs per point (direction + time). Measured on real
campaigns: pairwise spread 17.4 channels (9 points), 83.7 channels = 5.2 km/s (100 points), 77 channels (50 points).
The canonical axis is the ascending axis of the lowest-`point_index` point that carries velocity; every other point is
linearly interpolated onto it (an already-Doppler-corrected axis; no Doppler is recomputed). Rationale: all axes are the same
grid shifted by a per-point offset, so the choice only decides which ~1 % of edge channels lose support (reported), and it is
deterministic. A point whose axis already matches within 1e-3 channel is passed through untouched.

Resampling is explicit bracket-and-weight, not `np.interp`: a target bin is GOOD only if every source bin with non-zero weight is
GOOD and finite (an exact hit on a GOOD bin ignores its neighbours); otherwise MISSING. There is no tolerance (an earlier
tolerance emitted GOOD bins with NaN values). Consequences, measured: broad line (FWHM 100 ch) - FWHM error <= 0.014 %,
area exact, peak loss <= 1.4e-4; narrow line (FWHM 4 ch, worst case) - FWHM broadens up to 8.3 % and the peak loses up to
8 % at a half-channel shift, **area is conserved exactly**, centroid unchanged; an isolated masked bin widens to up to two
bins. Uncertainty: per-bin sigma is interpolated linearly with the same weights. Measured by Monte Carlo: per-channel sigma is
overstated by up to sqrt(2) at a half-channel shift (conservative), while the sigma of the INTEGRATED map is right (0.98-1.03),
because interpolation of white noise conserves the variance of the sum.

Resample QC persisted in `qc/resample_qc.json`: max/median offset (m/s, channels), pairwise spread, fraction of points
interpolated, edge bins lost, masked fraction before/after, stage timings.

## 5. NaN / validity policy

1. A masked or invalid value MAY be NaN (REDUCE stores NaN at masked bins).
2. NaN/Inf never contributes to any sum. Validity is applied **before** arithmetic by selection (`np.where`), never by a
   zero weight (`0*NaN = NaN`, `0*Inf = NaN`).
3. A voxel with no valid contributor is NaN, `weight_sum` 0, `valid=False` - never 0.
4. A finite `0.0` with a valid mask is a measurement, not missing (tested).
5. `GriddingAccumulator.add_point` enforces validity itself (`bin_valid & finite value & finite sigma & sigma >=
   SIGMA_ABSOLUTE_FLOOR`) and counts rejections (`n_nonfinite_rejected`); a non-finite/negative spatial weight is a loud
   `ValueError` (caller bug).
6. `SIGMA_ABSOLUTE_FLOOR = sqrt(2^20 / finfo.max) ~ 7.6e-152`: numerical overflow guard derived from the float64 range so
   2^20 weights of `1/sigma^2` cannot overflow. Variance is computed as `(sum w^2 s^2 / sum w) / sum w`, not `/ (sum w)**2`
   (that overflowed to sigma = 0).

## 6. Gridding equation and uncertainty

For pixel p, channel k, over input points i that pass the quality policy and have valid bin (i,k):

```
W_ik(p)   = w_beam(theta_i(p)) * 1 / sigma_ik^2          (no other factor)
y(p,k)    = sum_i W * v_ik / sum_i W                      (weighted mean; NaN where sum W = 0)
Var(y)    = sum_i W^2 sigma_ik^2 / (sum_i W)^2            (general formula, NOT 1/sum(W))
weight_sum = sum_i W ;  n_pointings = #{i : W > 0}
```
Verified against an independent Python-float evaluation (rtol 1e-12), exact inverse-variance cases (sigma = 1, 2, 10),
constant sky (bias 4e-16 with 4-25 overlapping pointings), zero sky, two-point overlap (value not doubled, sigma/sqrt(2)
exactly), Monte Carlo (no resampling: empirical/reported 0.999, truth within 1 sigma 0.685, within 2 sigma 0.954; with LSRK
offsets: 0.94, 0.715, 0.968) and a full forward model of the golden fixture (z rms 0.837, i.e. reported sigma ~16 %
conservative there).

**The reported uncertainty is propagated STATISTICAL uncertainty only.** REDUCE's sigma is a fixed per-bin profile; on real
data the between-point scatter is 2.1x that sigma (point-to-point baseline systematics), so SCIENCE sigmas are lower bounds.
**Neighbouring output pixels are beam-correlated** (measured noise correlation r = 0.74 for adjacent pixels): a per-pixel sigma
does not make neighbouring pixels independent; no covariance matrix is produced. Nothing is done about outliers: there is no
spatial sigma clipping (an extreme GOOD point contributes exactly as the formula says).

### Local sigma consistency (real-data finding)
Real REDUCE sigma has ~67 isolated bins per point 20-850x below the between-point scatter observed there (false precision,
also defeats per-channel SNR gating). A bin whose sigma is `< sigma_local_floor_fraction` (0.1) x the running median of its
129 neighbouring GOOD sigmas is **excluded and counted, not floored**, before resampling. Broad, genuinely low-noise stretches
are not flagged. Effect on real data: input masked fraction 4.19 -> 5.00 %, cube min sigma / median 0.004 -> 0.127.

### Quality policy (REDUCE point state -> usable)
| policy | GOOD | WARNING | BAD | UNKNOWN |
|---|---|---|---|---|
| STRICT | yes | no | no | no |
| STANDARD (default) | yes | yes | no | no |
| PERMISSIVE | yes | yes | no | yes |
BAD is never used. Masks apply independently of the policy.

## 7. Coverage semantics (four different things, never all called "coverage")
- `n_pointings`: pointings with non-zero weight at the voxel (hard cutoff -> integer ripple; unrelated to REDUCE's
  `n_contributing`, subintegrations per bin).
- `weight_sum`: sum of applied beam x inverse-variance weights (weighted support; a missing pointing lowers it without
  blanking a pixel that neighbouring beams still cover).
- `spectral_coverage_fraction` (integrated map): valid part of the REQUESTED velocity window / window width.
- Effective integration time: **not produced in V1** (beam-weighted seconds are not elapsed seconds; no fake seconds).

## 8. Integrated map and channel maps
```
I(y,x)   = sum_i overlap_i * v_i(y,x)                [relative_intensity_dimensionless * m/s]
sigma_I  = sqrt( sum_i (overlap_i sigma_i)^2 )        (independent-channel assumption)
spectral_coverage = sum_{valid i} overlap_i / (vmax - vmin);   valid iff >= min_spectral_coverage_fraction (0.5)
```
`overlap_i` = width of channel i's bin (edges = midpoints of the ACTUAL axis) inside `[vmin, vmax]`: exact for irregular axes,
windows off channel boundaries, windows narrower than one channel, and windows partly outside the cube (reported:
`window_status = PARTIAL_OUTSIDE_CUBE`, `window_covered_by_cube_fraction`); fully outside raises `BLOCKED`. Masked channels are
never interpolated (an incomplete line gives a lower integral and lower coverage). Negative values are legitimate. Measured
area error on analytic Gaussians: <= 1e-13 %. Channel maps: `channel_map` (interval, inclusive centres), `channel_map_nearest`;
selected indices/velocities are reported; a single-channel map equals the cube slice and sigma exactly.

`moment1_like` / `moment2_like` (deferred-grade, kept): gated by integrated SNR >= `moment_min_snr` AND per-channel
value >= `moment_min_snr` x channel sigma (clipping only negatives let window noise bias a high-SNR centroid by 1.3
channels; now 0.07). Beam smoothing shrinks recovered velocity gradients (golden: 4730 vs 8000 m/s/deg true, 4493 by the
independent model).

## 9. Units
`velocity_lsrk_m_s` m/s (LSRK); `relative_intensity` dimensionless fractional excess over the REDUCE baseline;
integrated map `relative_intensity_dimensionless * m/s`; moments m/s; angles degrees. **No absolute calibration of any kind**
exists in SCIENCE: no physical brightness or flux units, no column density; labels say "relative" / the exact unit.

## 10. Quality, completeness and status (separate axes)
- `status`: pipeline EXECUTION - `RUNNING -> COMPLETED | FAILED | CANCELLED`.
- `data_completeness`: `COMPLETE | PARTIAL` (PARTIAL if the REDUCE session is not COMPLETED or any point was excluded).
- Science `quality.state` GOOD/WARNING/BAD/UNKNOWN with `reasons`; separate `limitations` that never downgrade the state:
  `PROVISIONAL_BEAM_MODEL`, `VELOCITY_RESAMPLED` (expected LSRK geometry, not a defect), `INPUT_PARTIAL`.
  WARNING reasons: `INSUFFICIENT_POINTS`, `POINTS_EXCLUDED`, `LOW_SPATIAL_COVERAGE`, `INPUT_WARNINGS`, `UNCALIBRATED`,
  `INPUT_PARTIAL`, `SPECTRAL_COVERAGE_LOW`, `INTEGRATION_WINDOW_PARTIAL`. BAD: `NO_USED_POINTS`, `NO_VELOCITY`,
  `NO_VALID_VOXEL`, `NO_VALID_MAP_PIXEL`, `UNCERTAINTY_EXTREME` (median sigma >= 1 = noise >= 100 % of the reference level).
- Product status `VALID | PARTIAL | BLOCKED | FAILED` with reasons; BAD quality blocks every product.
- Verified: 8 adversarial scenarios -> 0 false-good; 3 healthy fixtures -> 0 false-bad.

## 11. Persistence and self-description
HDF5 is canonical (PNG is not). `cube/science_cube.h5` and `maps/*.h5` are written to `<name>.tmp` and renamed after a clean
close. Each file alone exposes: `science_schema_version`, `science_session_id`, `reduce_session_id`, `campaign_id`,
`input_reduce_manifest_sha256`, `config_hash`, `quality_state`, `data_completeness`, `coordinate_frame`, `axis_order`,
`shape`, `intensity_unit`, `velocity_unit`/`velocity_frame`/`velocity_axis_order` (cube), `beam_model`, `beam_fwhm_deg`,
`beam_source`, `beam_status`, `beam_json`, `grid_json`, `units`, spatial orientation. Cube arrays float64 (float32 would cost
3.7e-8 of the peak in intensity and 2e-6 in the integrated map - harmless, but a 2x disk saving does not justify a format
change now); contiguous, uncompressed (best for channel and full-cube reads; lzf/gzip save 28-33 % at 2.5-3 s write cost).

`manifest.json` (fields): `science_schema_version`, `science_session_id`, `input_reduce_session_id`,
`input_reduce_schema_version`, `input_reduce_session_status`, `input_reduce_manifest_sha256`, `campaign_id`, `config_hash`,
`status`, `data_completeness`, `beam`, `grid`, `velocity`, `n_points_*`, `integrated_window_m_s`, `quality`, `products`
(index: `id, kind, path, shape, unit, status, reasons, sha256, arrays_sha256`), `runtime`. `provenance.json` adds git commit,
software versions, and per-point `{point_index, used_in_cube, exclusion_reason, master_spectrum sha256s, capture_refs}` so any
pixel traces to science session -> config -> REDUCE manifest hash -> MasterSpectrum hashes -> CaptureRefs, without opening RAW.
`arrays_sha256` hashes only the scientific arrays, so two runs compare equal regardless of session-id attributes.
`config_hash` is a sha256 of canonical JSON (sorted keys, fixed separators): field order never changes it.

`validate_science_session` (read-only) verifies the manifest, schema (unknown -> refuse), status, config/provenance hashes,
every product's existence, sha256, HDF5 readability, dataset shapes/axis lengths, attributes, beam metadata, velocity
ascending, no unindexed canonical file, no leftover `.tmp`.

## 12. Memory model (measured)
Peak RAM = 57.1-57.5 B/voxel (4 accumulator arrays 32 B + finalize 24 B + valid 1 B; `add_point` works in 256-channel blocks) +
~100 MB baseline + 48 B per point-channel of ingested spectra. Disk = 33 B/voxel. `run_science_session` runs the preflight
before any allocation and before a session exists; budget = min(3 GiB, 60 % of MemAvailable).
