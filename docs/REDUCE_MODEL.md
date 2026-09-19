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
| `campaign_id`, `point_index`, `reduce_session_id` | identity - `reduce_session_id` is set by `ReduceSession.write_point()` at persist time (a bare `MasterSpectrum` in memory doesn't know its session yet) and is also written as an HDF5 attr, so a `master_spectrum.h5` opened alone (no JSON sidecar) is still traceable to its session |
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

## Units contract (V1, definitive)

One canonical storage unit per quantity - a future UI/SCIENCE layer
converts for display, REDUCE itself never stores an ambiguous unit.

| Field | Unit | Notes |
|---|---|---|
| `frequency_hz` | Hz | always the receiver's own topocentric grid |
| `velocity_lsrk_m_s` | m/s | canonical velocity storage unit is **m/s**, not km/s (`reduce_engine.velocity` computes km/s internally, per `hi_spectral_metric`'s own convention, then converts once at the MasterSpectrum boundary) |
| `ra_hours` | hours (0-24) | matches real capture metadata's own `target_ra_hours` field - never degrees, to avoid a silent x15 conversion bug at the boundary |
| `dec_degrees` | degrees (-90..90) | |
| `integration_time_seconds` | seconds | |
| `relative_intensity`, `uncertainty` | dimensionless fractional excess over a locally-fit or externally-referenced continuum | **never** Kelvin, dBm, or Jansky unless a future, explicitly-versioned absolute calibration path is added (not part of V1, see docs/REDUCE_SCOPE.md) |
| `calibration_level` | enum string | `RELATIVE` \| `UNCALIBRATED` only |
| `mask` | int64 bitmask | see MaskFlag above - not a physical unit |
| `n_contributing` | integer count | |

Not yet part of the V1 schema (documented as absent, not silently
omitted): AZ/ALT and Galactic l/b. OBSERVE's real metadata records
altitude/azimuth *at GOTO time* (`altitude_deg_at_goto`,
`azimuth_deg_at_goto`), which is a snapshot at slew time, not necessarily
valid at the capture's own timestamp - carrying it into MasterSpectrum
without recomputing for the actual capture time would be a quiet
inaccuracy, so V1 does not include it. Galactic l/b is a trivial
astropy-computable projection of RA/DEC and is deliberately left to
SCIENCE/presentation layers rather than duplicated here.

Persistence: metadata (`manifest_dict()`) as JSON, arrays as HDF5 - see
`reduce_engine/storage.py` and docs/REDUCE_PIPELINE.md's storage section.
No pickle, ever.

**Self-description**: `master_spectrum.h5` also carries its own HDF5
attrs (`reduce_schema_version`, `campaign_id`, `reduce_session_id`,
`point_index`, `calibration_level`, `velocity_frame`, `quality_state`,
and a small `units_json` string) so the file is identifiable and
interpretable in isolation - without its JSON sidecar, the session
manifest, or this doc. This is not a duplicate of the manifest: it's the
minimum needed to answer "what is this file, and what are its units."

## Schema/pipeline versioning

Every MasterSpectrum and every session manifest carries
`reduce_schema_version` (`reduce_engine.models.REDUCE_SCHEMA_VERSION`) and
every session's `provenance.json` carries `pipeline_version`
(`reduce_engine.config.PIPELINE_VERSION`), independent of the git commit
hash (also recorded).

**Versioning policy**: `reduce_schema_version` is currently `"1.0"`.
Backward-compatible additions (a new optional field, a new QC metric) may
land as `"1.x"`. Any change that removes a field, changes a unit, or
changes the meaning of an existing field requires `"2.0"`. SCIENCE (via
`reduce_engine.science_contract.validate_science_input`) must reject an
unrecognized schema version outright rather than guess its meaning - this
is enforced today (see `test_reduce_science_contract_and_integrity.py`).

## Future: absolute calibration contract (not implemented)

If CALIBRATE ever produces a validated absolute-temperature calibration,
REDUCE would need (not implemented, no code path exists today):

- `calibration_level = "ABSOLUTE"` as a new enum value (`MasterSpectrum`'s
  own validation currently hard-rejects anything but `RELATIVE`/
  `UNCALIBRATED` - this is a deliberate, explicit gate, not an oversight).
- Units would change from dimensionless fractional excess to Kelvin (or
  Jy, if a flux calibration), stated explicitly per-point, never mixed
  with relative-only points in the same session.
- A per-bin uncertainty in the same absolute unit, propagated through
  from CALIBRATE's own absolute-calibration uncertainty budget - not
  invented by REDUCE.
- A `calibration_profile` reference to the absolute profile, versioned
  and hashed exactly like the relative case today.

Today, unconditionally: `RELATIVE` or `UNCALIBRATED`, dimensionless
fractional excess only.
