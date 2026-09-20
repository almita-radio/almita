# SCIENCE: Pipeline (V1)

## Canonical order (`science_engine.products.run_science_session`)

```
INGEST (reduce_engine.science_contract gate + per-point checks + sha256)
  -> BUILD BEAM + GRID -> PREFLIGHT (RAM/disk, blocks BEFORE any cube-sized allocation)
  -> CREATE SESSION (fail closed on collision; manifest status RUNNING)
  -> CUBE (sigma-local check, ascending canonical axis, resample, beam x 1/sigma^2 accumulation)
  -> INTEGRATED MAP -> MOMENT-LIKE MAPS -> QUALITY
  -> PERSIST (atomic HDF5) -> INDEX (sha256) -> MANIFEST status COMPLETED (the commit point)
```

Crash safety: any exception rewrites the manifest `FAILED` (`CANCELLED` for KeyboardInterrupt, CLI exit 130) and
re-raises; HDF5 files are written to `.tmp` and renamed, so no half-written canonical file can exist. Two separate status
axes: `status` (execution) and `data_completeness` (COMPLETE|PARTIAL).

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

`inspect`, `plan` (no writes; grid/beam, RAM/disk estimate, MemAvailable, checks), `run`, `replay`, `compare`,
`validate`, `status`. `--json` everywhere. Beam mandatory (see runbook).

## Performance (measured on this Pi; see SCIENCE_ACCEPTANCE.md)

| session | points | grid x channels | runtime | peak RSS | output |
|---|---|---|---|---|---|
| 9-pt real | 9 | 21x21 x 8192 | 2.3 s | 282 MB | 119 MB |
| PARTIAL real (50 of 100 COMPLETED) | 50 | 33x56 x 8192 | 31.2 s | 928 MB | 500 MB |
| 100-pt real | 100 | 49x51 x 8192 | 77.6 s | 1237 MB | 676 MB |
| synthetic stress | 9 | 50x50 x 8192 | 9.5 s cube | 1194 MB (est. 1213) | 645 MB |

Top bottlenecks (100-pt real): cube accumulation 70.6 s of 77.6 s (91 %), persistence 3.0 s, integrated products 1.4 s.
Accumulation cost is ~ (points x voxels); restricting each point to the bounding box of its non-zero beam support would give
bit-identical results (adding exact zeros) with a large speedup - **not applied** (not pathological; frozen semantics).

Memory: 57 B/voxel + ~100 MB + 48 B per point-channel; accumulation loops over points and works in 256-channel blocks.

## Known limitations

See `docs/SCIENCE_SCOPE.md`'s "Explicitly out of scope for V1" section
for the full list (l-v diagrams, FITS export, real-survey reference
comparison, cross-campaign stacking, dedicated `compare`/`replay` CLI
commands) and "Real-data findings that shaped V1" for the two genuine
bugs/gaps this pass's own real-data audit caught before they could reach
a user: velocity-axis incompatibility across points, and the NaN-value/
zero-weight poisoning bug in `GriddingAccumulator`.
