# SCIENCE: Scope (V1)

SCIENCE sits between REDUCE and the future PRESENTATION layer:

```
OBSERVE -> REDUCE -> SCIENCE -> PRESENTATION (future, web/PNG/reports)
```

SCIENCE's only input is one REDUCE session directory (frozen at commit
`2afc4c5`, see `docs/REDUCE_V1_FREEZE.md`). It must be possible to run
SCIENCE on a machine that has ONLY that REDUCE session copied onto it -
no `data/mosaic/`, no RAW IQ, no OBSERVE/CALIBRATE/ALIGN runtime state,
no hardware. If SCIENCE ever needs to open a raw HDF5, the architecture
is wrong (operator's own principle, restated here as a hard rule).

## What SCIENCE IS

SCIENCE transforms **LEVEL 1 (REDUCED SPECTRA)** into **LEVEL 2 (SCIENCE
PRODUCTS)**:

- a beam-weighted spatial grid over the input points' real sky extent
- a velocity-resolved cube (`relative_intensity`, `uncertainty`,
  `weight_sum`, `n_contributing`, `valid`)
- an integrated ("moment-0-like") relative intensity map over an
  explicit velocity window
- moment-1-like (velocity centroid) and moment-2-like (velocity
  dispersion) maps, gated on signal significance
- channel maps (single-velocity-interval slices)
- coverage, uncertainty, and quality QC products
- a SCIENCE-level quality assessment, kept separate from REDUCE's own
  per-point quality
- full provenance back to the REDUCE session and, transitively, to RAW

## What SCIENCE is NOT

- SCIENCE does not capture. No hardware, no INDI, no `rtl_tcp`, no mount.
- SCIENCE does not FFT. No spectral estimation from IQ.
- SCIENCE does not recalibrate. It consumes REDUCE's own
  `calibration_level`/`relative_intensity` as-is.
- SCIENCE does not recompute Doppler from RAW. It consumes REDUCE's own
  `velocity_lsrk_m_s` per point - see "Velocity axis resampling" below
  for the one interpolation operation V1 *does* perform, and why that is
  a different thing.
- SCIENCE does not make astrophysical claims. No "HI detected," no
  "Milky Way arm," no source identification. It produces measurement
  products; interpretation is a human/future step (section 47/101 of
  the operator's own brief).
- SCIENCE does not produce absolute-unit maps. Every intensity value is
  dimensionless relative excess - never Kelvin, Jy, dBm, Tsys, Tant,
  SEFD, or physical column density - unless REDUCE itself someday
  delivers `calibration_level=ABSOLUTE` (not implemented, no code path
  exists anywhere in this repo today).

## Real-data findings that shaped V1 (not assumptions)

Two genuine discoveries from auditing REDUCE's real, frozen output
(`data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411`,
produced fresh for this pass) directly shaped what V1 does:

1. **Velocity axis resampling is required, not optional.** REDUCE
   guarantees a common `frequency_hz` grid within a campaign (confirmed:
   identical across all 9 real points), but `velocity_lsrk_m_s` is
   direction+time dependent - measured up to 641 m/s (10.4 channels) of
   real disagreement between points in this exact 9-point campaign.
   Gridding by channel index without resampling first would silently
   misalign the line by several channels across the field. See
   `science_engine/resample.py`'s own docstring and
   `docs/SCIENCE_MODEL.md`'s Velocity section.
2. **A masked bin's `relative_intensity` is NaN in real data, always**
   (confirmed: 343/343 masked bins on a real point are NaN, 0 GOOD bins
   are). This caught a real bug in this pass's own first implementation
   of `GriddingAccumulator` - `0 * NaN == NaN` in IEEE754, so a masked
   bin's weight being correctly zero did NOT stop its NaN value from
   poisoning the running sum via `+=`. Fixed (`science_engine/gridding.py`);
   the entire real-run integrated map was silently NaN before the fix
   and is real, finite data after it. See "Known limitations" in
   `docs/SCIENCE_V1_FREEZE.md`-equivalent acceptance notes for the full
   story - this is exactly the class of bug a synthetic-only test suite
   can miss (the original synthetic fixtures never set NaN at masked
   positions; `science_engine/simulation.py` now does, matching real data).

## Beam FWHM: a documented gap, not a guess

REDUCE's frozen contract carries NO beam/grid metadata into
`MasterSpectrum`, the session manifest, or provenance - confirmed by
reading a real session's `manifest.json`/`master_spectrum.json`/
`provenance.json` end to end. The ACTUAL grid spacing used to plan a
real campaign lives in `data/mosaic/<campaign>/grid_metadata.json`
(upstream, off-limits to SCIENCE by this document's own first rule).
This repo also has two OTHER historical beam-FWHM values that disagree
with each other (`alignment_engine/config.py`'s own documented
14.0/20.0 discrepancy). SCIENCE V1 does not resolve it and does not pick
one silently: the beam is **operator-provided operational metadata**
(`--beam-fwhm-deg`, recorded as `source=operator_config`; or
`--beam-from-observer-config [PATH]`, which records the file's path, field
and sha256). With neither, the CLI refuses. The library default is a
labelled placeholder (`PROVISIONAL_DEFAULT`), never read from a file. The
real runs of the second pass used the observation grid's own
`beam_fwhm_deg` (1.5, 1.1111 and 3.3333 deg for the three real sessions),
passed by the operator: the grid spacing configured for OBSERVE, a
configured beam assumption, and a physical beam measurement are three
different things, and only the first two exist. Future improvement (schema
V2, not now): carry explicit beam metadata into Level 1.

## Explicitly out of scope for V1

- Hardware control, network access, and a web UI - same rules as REDUCE.
- Cross-campaign / multi-REDUCE-session stacking (section 86): one
  REDUCE session -> one SCIENCE session, always.
- l-v (Galactic longitude x velocity) diagrams: deferred, not implemented. The 9-point real fixture's Galactic longitude coverage is
  too narrow to demonstrate one meaningfully, and this pass prioritized
  the cube/map/moment core over an additional product family.
- FITS export (sections 62, 125-127): not implemented. HDF5 is V1's only
  persisted format - see `docs/SCIENCE_MODEL.md`'s HDF5-vs-FITS section
  for what a future FITS exporter would need.
- Real HI4PI/LAB-survey reference comparison (sections 102-104): this
  repo has NO real survey FITS file anywhere (confirmed by
  `alignment_engine/targets/hi_reference.py`'s own honest
  `FITSHIReferenceProvider` stub, which already documents this same gap
  for ALIGN). SCIENCE follows that exact precedent rather than
  reinventing a different answer: not implemented, explicitly, until a
  real local survey file exists.
- Moment-like products exist but are gated by signal (see SCIENCE_MODEL.md); they are not a claim of professionally
  reduced moment maps.

`compare` and `replay` ARE implemented in the second pass (see SCIENCE_PIPELINE.md).
