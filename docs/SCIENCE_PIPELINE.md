# SCIENCE: Pipeline (V1)

## Canonical order (`science_engine.products.run_science_session`)

```
INGEST (reduce_engine.science_contract gate) -> VALIDATE CONTRACT
  -> BUILD BEAM + GRID -> VALIDATE/RESAMPLE VELOCITY AXIS
  -> GRID SPECTRA INTO CUBE -> DERIVE INTEGRATED MAP
  -> DERIVE MOMENT-1/2-LIKE MAPS -> ASSESS SCIENCE QUALITY
  -> PERSIST -> (VALIDATE OUTPUT via `almita_science.py validate`)
```

An exception propagates BEFORE `manifest.json` is written - a session
directory can exist with partial products on disk, but `status` is
never falsely `"COMPLETED"`, mirroring `reduce_engine.pipeline`'s own
crash-recovery contract exactly (sections 110-112). A `KeyboardInterrupt`
is not caught anywhere in this path either, for the same reason.

## Storage layout

```
data/science/CAMPAIGN_ID/SCIENCE_SESSION_ID/
    manifest.json config.json provenance.json
    grid/            (reserved for V1; grid metadata is embedded in
                       manifest.json and each cube/map's own HDF5 attrs)
    cube/science_cube.h5
    maps/integrated_relative_intensity.h5
    maps/moment1_like_velocity_centroid.h5
    maps/moment2_like_velocity_dispersion.h5
    spectra/         (reserved for V1 - no per-point spectral re-export
                       exists yet; use qc_plots.plot_representative_spectrum
                       directly against the REDUCE session for that)
    qc/science_quality.json qc/coverage.json
    logs/events.jsonl logs/session.log
```

Immutable once created - same `SCIENCE_SESSION_ID` raises
`FileExistsError`, never overwrites (section 54). Campaign/session IDs
are checked against path traversal (`..`, embedded `/`, absolute paths,
null bytes) and the resolved path is re-verified to stay under
`output_root` even through a symlinked root - the exact pattern already
validated in `reduce_engine.storage`, re-implemented (not imported) per
section 113's "reuse a validated pattern" instruction since REDUCE's own
function is module-private. See `test_science_storage_and_security.py`.

## Quality policy (section 28-29)

`ScienceConfig.quality_policy` gates which of a REDUCE point's own
quality states are usable at all (whole-point gate, since REDUCE assesses
quality once per `MasterSpectrum`, never per-bin):

| policy | usable REDUCE quality states |
|---|---|
| `STRICT` | `GOOD` only |
| `STANDARD` (default) | `GOOD`, `WARNING` - REDUCE already demoted WARNING with an explicit, documented reason; SCIENCE trusts that reason rather than re-excluding it by default |
| `PERMISSIVE` | `GOOD`, `WARNING`, `UNKNOWN` |

`BAD` is never usable under any policy. Mask-level exclusion (a masked
bin never contributes, regardless of its point's quality) is completely
independent of this policy and always applied (section 27).

## Velocity window (sections 35-36)

Explicit only in V1 - `ScienceConfig.velocity_window_min_m_s` /
`_max_m_s` (default +-100,000 m/s), no auto-detection. Below
`min_spectral_coverage_fraction` (default 0.5) of the window's channels
being valid for a given pixel, that pixel's integrated value is
`NaN` - never extrapolated from a partial window (section 40).

## CLI (`almita_science.py`)

`inspect` (read-only contract+summary), `plan` (no writes; grid/beam
shape, memory/disk estimate, blocking checks), `run` (build+persist),
`validate` (output integrity for a finished session), `status`
(pipeline/schema version). Mirrors `almita_reduce.py`'s own command set
and `--json`/human-summary convention. No `compare`/`replay`/
`export-fits` subcommands in V1 - see `docs/SCIENCE_SCOPE.md`.

## Performance (measured, real 9-point campaign)

`data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411`
(9 real points, `--beam-fwhm-deg 1.5` matching this campaign's own known
grid spacing, default 4 px/beam -> 21x21 grid, 8192 velocity channels
carried through unchanged from REDUCE):

| metric | value |
|---|---|
| wall time | 2.8-3.0s (two independent measured runs) |
| peak RSS | 274.5 MB |
| output size | 115 MB (`estimate_output_bytes` predicted ~138 MB - same order, conservative) |

`science_engine/validation.py::run_preflight` estimates peak memory
BEFORE allocation (`8x` a single `(Nv,Ny,Nx)` float64 array - the
`GriddingAccumulator` holds 4 such arrays at once plus ~4 transient
finalize-step arrays) so a pathological grid request (e.g. a 4000x4000
spatial grid at full REDUCE spectral resolution) is caught and blocked
at `plan` time, never attempted (sections 119, 123).

Accumulation loops over INPUT POINTS, not a single
`(N_points, N_pixels, N_velocity)` tensor - peak memory scales with the
OUTPUT cube size only, never with how many points contributed to it
(`science_engine/gridding.py`'s own docstring).

## Known limitations

See `docs/SCIENCE_SCOPE.md`'s "Explicitly out of scope for V1" section
for the full list (l-v diagrams, FITS export, real-survey reference
comparison, cross-campaign stacking, dedicated `compare`/`replay` CLI
commands) and "Real-data findings that shaped V1" for the two genuine
bugs/gaps this pass's own real-data audit caught before they could reach
a user: velocity-axis incompatibility across points, and the NaN-value/
zero-weight poisoning bug in `GriddingAccumulator`.
