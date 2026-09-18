# Calibration V1 — Architecture & Model

Companion to `docs/CALIBRATION_SCOPE.md` (read that first for what may and
may not be claimed). This document covers how `calibration_engine/` is put
together, and what would need to change to go beyond it.

## Module map

```
calibration_engine/
  calibration_level.py    CalibrationLevel enum, OPERATIONAL_RELATIVE/ABSOLUTE_RF_UNAVAILABLE
  state_machine.py         CalibrationState: IDLE..COMPLETED/FAILED/CANCELLED/BLOCKED
  session.py                CalibrationSession: data/calibration/CAL-<ts>/, events.jsonl + session.log
  receiver_config.py        ReceiverConfigSnapshot, KNOWN_RECEIVERS (MAIN/RFI_REF identity)
  acquisition.py             Real/SimulatedCalibrationAcquisitionBackend (Protocol-based, like HI's)
  sample_statistics.py      Raw IQ statistics, streaming/chunked (O(256) memory)
  clipping.py                ClippingStatus + objective, configurable thresholds
  bandpass.py                 Normalized bandpass, DC/edge/spur characterization, mask candidates
  stability.py                 Temporal stability, warm-up detection, sigma-vs-integration-time law
  thermal.py                    DS18B20 integration + correlation (never mixed into RF calibration)
  gain_sweep.py                 Candidate gain table, sweep plan, linearity, recommendation
  frequency_axis.py             Offline axis validation vs. absolute-frequency status
  quality.py                     OperationalCalibrationQuality: GOOD/MARGINAL/BAD/INCONCLUSIVE
  comparability.py               compare_calibration_context / compare_calibration_sessions
  profile.py                      CalibrationProfile DRAFT + OperationalEnvelope, versioned
  replay.py                       Re-analyze existing captures, immutable source
  simulation.py                   Synthetic instrument behavior for tests (no hardware)
```

Reused, not duplicated: `hi_spectral_metric.py` (PSD/DC/spur primitives),
`calibration_foundation.py` (ADC statistics pattern, relative-profile
precedent), `temperature_sensors.py` (DS18B20Reader), `sdr_capture.py`
(the only thing that ever talks to rtl_tcp), `alignment_engine.deployment_state`
(the DeploymentState enum, as a label here — see Scope doc).

## State machine

`IDLE -> PLANNED -> PREFLIGHT -> READY -> ACQUIRING -> ANALYZING -> RESULT_READY -> COMPLETED`,
with `FAILED`/`CANCELLED` reachable from any non-terminal state and
`BLOCKED` reachable only from `PREFLIGHT`. `ANALYZING -> ACQUIRING` loops
back for a multi-step gain sweep or stability run before the session's
own final `RESULT_READY`. Same explicit-enum-plus-table pattern as
`alignment_engine/state_machine.py`, deliberately re-derived rather than
shared — calibration's happy path is different enough (no tracking, no
SYNC, a real ACQUIRING phase that means "one more capture," not "one more
raster point") that sharing the table would blur two separate concerns.

## Quality model

`OperationalCalibrationQuality` requires ALL of: complete metadata,
clipping not CLIPPED/UNKNOWN, sufficient valid samples, acceptable
stability, sufficient usable band, no severe RFI contamination — for
GOOD. Any single missing required input (stability or bandpass
unavailable) returns INCONCLUSIVE, never a guess. See `quality.py` for
the exact numeric thresholds and why each is round/defensible rather than
tuned to any specific historical result.

## Gain sweep methodology

1. `CANDIDATE_GAIN_TABLE_DB` — the published R820T2/R828D 32-step table
   (real values, but their applicability to THIS specific unit is
   `GAIN_TABLE_PROVENANCE`-flagged as never independently verified — see
   Fase 9's audit result in `gain_sweep.py`'s own docstring).
2. `build_gain_sweep_plan()` picks low/mid/high steps around a nominal
   gain from that real table — never an invented list.
3. Each step's `RawSampleStatistics`/`ClippingResult` are computed the
   SAME way regardless of gain (`compute_gain_step_result` uses
   `std_i`/`std_q`, which are AC-coupled by construction — using raw
   ADC-code RMS here was tried and found to be dominated by the DC
   pedestal, staying flat across gain and making a real sweep look
   falsely non-monotonic; this is documented in the function's own
   docstring as a found-and-fixed mistake, not a hypothetical).
4. `analyze_gain_linearity()` flags plateaus/discontinuities/reversals/
   clipping onset in gain-vs-power — relative only, no RF dB claim.
5. `recommend_operational_gain()` prefers the HIGHEST gain with
   COMFORTABLE headroom margin (a multiple of the WARNING threshold, not
   just clipping-free) — verified by test to neither default to the
   highest raw gain (which can be clipped) nor the lowest (excess
   caution) when a clearly better middle option exists.

## Comparability

`compare_calibration_context()`: different receiver or different sample
rate → `NOT_COMPARABLE` outright (hard disqualifiers). Different gain,
Bias-T, deployment context, or a >5°C mean temperature difference →
flagged as `PARTIALLY_COMPARABLE`, not disqualifying by itself.
`compare_calibration_sessions()` layers a `CONSISTENT`/`MARGINAL`/
`INCONSISTENT` verdict on top, using fractional RMS/clipping-fraction
deltas with the same "documented convention, not invented per comparison"
spirit as `alignment_engine/hi/repeatability.py`'s sigma bands.

## Profile / Operational Envelope

Every `CalibrationProfile` this pass can produce is `DRAFT` — there is no
code path that sets `ACTIVE` (Fase 62: that is a future, human, separate
decision). A profile is traceable to its `source_calibration_sessions`
(Fase 54) and its `code_commit` (Fase 55), and is never overwritten once
written (`write_profile()` raises on collision). `OperationalEnvelope`
records what "normal" looked like when the profile was built (gain range
tested, temperature range observed, clipping ceiling, expected stability,
usable band, known artifacts) so a LATER observation can be classified
`WITHIN_ENVELOPE`/`MARGINAL`/`OUTSIDE_ENVELOPE`/`UNKNOWN` — advisory only
in V1 (Fase 42/67: nothing aborts automatically on `OUTSIDE_ENVELOPE`).

## Audit findings baked into this design (Fase 63-65)

- **40.2 dB** is a deeply embedded historical default across this repo
  (`sdr_capture.BASELINE_GAIN_DB`, `alignment.DEFAULT_GAIN_DB`,
  `alignment_engine/config.py`'s `gain_db` default, and multiple
  standalone RF test scripts) — this pass does not change any of them; it
  only adds the machinery to evaluate whether 40.2 is actually the right
  operational choice, with evidence, without assuming the answer.
- **Bias-T** state is recorded per session (`bias_t_state`) and is one of
  `compare_calibration_context()`'s flagged (not disqualifying)
  dimensions — a Bias-T-OFF session is never silently pooled with a
  Bias-T-ON one.
- **MAIN vs RFI_REF** are different receiver identities
  (`KNOWN_RECEIVERS`), and `compare_calibration_context()` hard-disqualifies
  any comparison across them. A profile is built `for` one `receiver_id`
  and never silently applied to the other.
- **rtl_tcp has no gain/serial/PPM readback** — confirmed by grep audit of
  `sdr_capture.py` (no `0x05` PPM command, no serial query). Every
  "requested" value this pass records is exactly that: requested, not
  independently confirmed by the hardware.

## What would unlock Absolute Calibration (Fase 52) — no shopping, just requirements

**TIER 1 — cheap/simple** (would let us start bounding, not fully
calibrating, absolute quantities):
- A physically-measured 50-ohm termination temperature (a thermometer on
  the actual load, not an assumed "room temperature") — this alone,
  combined with the existing `calibration_foundation.py` 50-ohm reference
  ensemble, would let a Y-factor-style estimate begin, still with wide
  uncertainty.
- A second, independently-known 50-ohm load at a different physical
  temperature (e.g., one warmed/cooled deliberately) — turns "one cold
  point" into a real two-point Y-factor measurement.

**TIER 2 — meaningfully better**:
- A calibrated noise source with a documented ENR (Excess Noise Ratio)
  across the band of interest — the standard hot/cold radiometric
  calibration input; this is the single highest-value piece of equipment
  for this project's actual purpose (HI-line radiometry).
- A step attenuator with known, traceable attenuation steps — lets gain
  linearity be measured in real RF dB, not just relative digital power.

**TIER 3 — real metrology**:
- A signal generator with a known, traceable output power at the HI line
  frequency — enables absolute power/gain calibration and true P1dB
  characterization.
- A power meter (traceable) — closes the loop on any of the above without
  relying on the SDR's own (uncalibrated) ADC as the only power reference.
- A VNA, where cable/connector loss and impedance matching matter (LNA
  input matching, cable runs) — not needed for a first radiometric
  calibration, but needed to trust any S-parameter-derived loss number.

None of this is purchased or scheduled by this pass — it is the technical
answer to "what would it take," recorded so a future decision has a real
starting point instead of re-deriving it from scratch.

## Future extension points, deliberately left disabled (Fase 30-32, 53, 69)

- `InputCondition`-style metadata (ANTENNA / 50_OHM_TERMINATION /
  NOISE_SOURCE / OPEN / KNOWN_GENERATOR) is not yet a first-class enum in
  `calibration_engine` — `calibration_foundation.py`'s own
  `reference_topology` free-text field already carries this informally
  (see its `"50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR"` style values); a
  future pass should promote it to an explicit enum rather than
  reinventing the concept.
- `thermal.SensorCalibration` (per-ROM offset/slope) exists as a
  dataclass with `calibrated=False` by construction — `apply()` returns
  `None` until a real thermal reference measurement sets `calibrated=True`
  with real `offset_c`/`slope` values. Never touched by this pass.
- Hot/Cold/noise-source calibration (`reference_temperature`, `ENR`,
  `known_input_power`, `measured_output`) has no module yet — Tier 2/3
  equipment above is the prerequisite, not a missing afternoon of coding.
