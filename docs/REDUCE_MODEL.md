# REDUCE: Data Model

## Terminology (precise, never conflated)

- **RAW** - the original HDF5 capture, as written by `sdr_capture.py`.
  Never modified.
- **SPECTRUM** - a PSD estimate from one capture's IQ, in the receiver's
  own linear power units. Not calibrated, not baseline-corrected.
- **CALIBRATED RELATIVE SPECTRUM** - a spectrum after
  `apply_relative_calibration_to_psd` (calibration_foundation.py) has been
  applied against a compatible profile. Still not baseline-corrected.
  `calibration_level = "RELATIVE"`.
- **BASELINE-CORRECTED SPECTRUM** - a spectrum (calibrated or not) with a
  low-order continuum model subtracted/divided out, expressed as
  `relative_intensity` (a fractional excess over the continuum). Never
  called "calibrated" on its own if no external profile was used.
- **MASTER SPECTRUM** - the final, stacked, per-point LEVEL 1 product.
  See below.
- **RFI MASK** - the subset of the per-bin mask contributed by RFI_REF
  coincidence (`MaskFlag.RFI`), distinct from `KNOWN_SPUR` (a persistent
  narrow feature detected within one capture's own sub-integrations,
  without any external reference).
- **UNCERTAINTY** - a 1-sigma-scale per-bin error estimate, always
  distinguishable from "noise" (a property of the data) vs "uncertainty
  of the estimate" (a property of the measurement).
- **VELOCITY FRAME** - one of `topocentric`, `heliocentric`,
  `barycentric`, `lsrk`, or `UNAVAILABLE`. Never silently defaulted.

## CaptureRef

```python
CaptureRef(campaign_id, point_index, capture_id, source_path, timestamp_utc,
          sha256, receiver, metadata_source)
```

Identity of one raw capture. Frozen (immutable) once constructed. Every
LEVEL 1 product traces back to one or more `CaptureRef` -> the original
raw file, by path and content hash (`sha256`).

## MaskFlag

A `Flag` enum (bitmask), so one bin can carry multiple simultaneous
reasons:

`GOOD (0)`, `DC`, `KNOWN_SPUR`, `RFI`, `EDGE`, `INVALID`, `MISSING`,
`SATURATED`, `USER_EXCLUDED`.

A bin is usable in any fit/average only if its flag is exactly `GOOD` -
`.usable()` / `masks.usable_bin_mask()`.

## QualityReport

```python
QualityReport(state, reasons: list[str], metrics: dict)
```

`state` is one of `GOOD`, `WARNING`, `BAD`, `UNKNOWN`. `reasons` is never
empty, even for `GOOD` - see docs/REDUCE_PIPELINE.md's quality section for
the exact rules.

## MasterSpectrum (the LEVEL 1 contract to SCIENCE)

| field | meaning |
|---|---|
| `campaign_id`, `point_index` | identity |
| `ra_hours`, `dec_degrees` | pointing (as declared by OBSERVE) |
| `timestamp_start_utc`, `timestamp_end_utc` | real capture timing |
| `frequency_hz[]` | canonical frequency axis |
| `velocity_lsrk_m_s[]`, `velocity_frame` | velocity axis, or `UNAVAILABLE` |
| `relative_intensity[]` | fractional excess over continuum (see terminology) |
| `uncertainty[]` | per-bin 1-sigma estimate |
| `mask[]` | per-bin `MaskFlag` bitmask |
| `n_contributing[]` | how many captures contributed to each bin |
| `integration_time_seconds` | real, declared integration time |
| `quality` | `QualityReport` |
| `calibration_level`, `calibration_profile_id`, `calibration_profile_hash` | never Kelvin/Jy/etc |
| `capture_refs[]` | provenance back to RAW |

Units: `frequency_hz` in Hz, `velocity_lsrk_m_s` in m/s,
`relative_intensity`/`uncertainty` dimensionless fractional excess over a
locally-fit or externally-referenced continuum - **never** Kelvin, dBm,
or Jansky unless a future, explicitly-versioned absolute calibration path
is added (it is not part of V1, see docs/REDUCE_SCOPE.md).

Persistence: metadata (`manifest_dict()`) as JSON, arrays as HDF5 - see
`reduce_engine/storage.py` and docs/REDUCE_PIPELINE.md's storage section.
No pickle, ever.

## Schema/pipeline versioning

Every MasterSpectrum and every session manifest carries
`reduce_schema_version` (`reduce_engine.models.REDUCE_SCHEMA_VERSION`) and
every session's `provenance.json` carries `pipeline_version`
(`reduce_engine.config.PIPELINE_VERSION`), independent of the git commit
hash (also recorded).
