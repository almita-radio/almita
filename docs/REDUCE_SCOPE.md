# REDUCE: Scope (V1)

REDUCE sits between OBSERVE and the future (not yet implemented) SCIENCE
module:

```
OBSERVE -> REDUCE -> SCIENCE (future)
```

CALIBRATE feeds REDUCE. RFI_REF feeds REDUCE (veto/flag only in V1).

## What REDUCE IS

REDUCE transforms **LEVEL 0 (RAW capture, evidence)** into **LEVEL 1
(REDUCED SCIENTIFIC MEASUREMENT)**. For each observed point it produces:

- a scientifically usable spectrum, with a well-defined frequency axis
- a velocity axis when the required metadata is available
- a formal, multi-reason bin mask (never a bare boolean)
- applied *relative* calibration (never absolute)
- a treated (baseline-corrected) spectrum
- averaged/stacked captures, when more than one contributes to a point
- a per-bin noise/uncertainty estimate, plus global QC metrics
- explicit quality state and reasons
- complete provenance and a reference back to the original raw captures

## What REDUCE is NOT

REDUCE's responsibility ends **before** producing final spatial science
products. It never produces:

- final spatial heatmaps or maps
- RA x DEC x velocity cubes
- galactic/astrophysical interpretation
- automatic structure detection
- "science conclusions" of any kind

That is SCIENCE's job, once SCIENCE exists.

## The four LEVELs

| Level | Name | Owner | Mutable? |
|---|---|---|---|
| 0 | RAW / EVIDENCE | OBSERVE | **Never** |
| 1 | REDUCED SPECTRA | **REDUCE** | Never (new session instead) |
| 2 | SCIENCE PRODUCTS | SCIENCE (future) | - |
| 3 | PRESENTATION | web/PNG/reports | - |

REDUCE works **only** LEVEL 0 -> LEVEL 1.

## The golden rule

**RAW ES EVIDENCIA.** REDUCE never modifies an original capture file,
never overwrites IQ, never rewrites an original HDF5, never "cleans"
original data. A bad bin is MASKed, never zero-filled. A bad capture is
QUALITY FLAGged, never silently dropped. A step that cannot execute is
BLOCKED or reports UNKNOWN - REDUCE never invents a value it does not
have evidence for.

## Explicitly out of scope for V1

- Hardware control of any kind (no SDR reconfiguration, no GOTO, no SYNC,
  no INDI, no rtl_tcp). REDUCE never imports hardware-control modules.
- Network access of any kind. `run`/`replay`/`compare`/`plan`/`inspect`
  are 100% offline, filesystem-only operations.
- A web UI (`reduce.html` comes only after this contract is frozen).
- Anything SCIENCE's job (see above).
- Adaptive RFI cancellation - RFI_REF is VETO/FLAG only in V1.
- Absolute calibration (Kelvin, Jansky, Tsys, Tant, SEFD, NF, dBm-referenced
  power). Every REDUCE output's `calibration_level` is `RELATIVE` or
  `UNCALIBRATED`, never anything absolute.

## Contract to the future SCIENCE module

SCIENCE should only ever need: the REDUCE session manifest, MASTER
SPECTRA (one per point), point coordinates, masks, uncertainty, quality,
and provenance. SCIENCE should **never** need to reopen raw IQ, redo the
FFT, recalibrate, or redo the Doppler correction. If it does, the
architecture between REDUCE and SCIENCE is wrong.
