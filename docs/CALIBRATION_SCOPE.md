# Calibration V1 Scope — what ALMITA can and cannot measure today

This document is the contract `calibration_engine/` is built against. It exists
because "calibration" is an overloaded word, and this project has already
produced code (`calibrate.py`, an unfinished Hot/Cold/Load script) that
*implies* absolute radiometric calibration without the metrological chain to
back it. This pass does not build that. It builds the thing that is actually
possible right now: knowing whether ALMITA is behaving today the way it
behaved yesterday, in units that make no claim beyond what was measured.

## The one-sentence definition

**Calibration V1 = OPERATIONAL / RELATIVE / REPRODUCIBILITY calibration.**
It answers "is this instrument, in this configuration, behaving consistently
and sanely" — never "how many Kelvin/Jansky is this."

Every result this pass produces carries `calibration_level` and
`absolute_calibration: false` explicitly, following the precedent already set
by `calibration_foundation.py` (`CALIBRATION_LEVEL = "RELATIVE_INSTRUMENTAL"`,
`absolute_calibration: False` enforced at both write and read time). This
pass generalizes that pattern under `calibration_engine.CalibrationLevel`
rather than replacing it.

## We DO NOT have (and this pass will not fake)

- Absolute RF gain chain (dB from antenna terminal to ADC code)
- System temperature (Tsys), in Kelvin
- Noise figure (NF)
- Calibrated antenna temperature
- Flux density in Jansky
- SEFD
- Absolute frontend compression point (P1dB in real dBm)
- Absolute cable loss
- Absolute LNA gain
- Absolute bandpass power (real dBm/Hz)
- Absolute system sensitivity

None of these can be produced without a metrologically-known reference
(a calibrated noise source with known ENR, a known 50-ohm physical
temperature actually measured, a step attenuator, a signal generator with
known output power, or a power meter) — see `CALIBRATION_MODEL.md`'s
"What would unlock Absolute Calibration" section for the concrete equipment
tiers. **We do not have any of that equipment today**, and RFI_REF is
explicitly not a substitute — it is a second receiver of the same
uncharacterized kind, not a reference standard.

Any code, doc, or JSON field in this pass that could be read as one of the
above IS A BUG. See "Naming rules" below.

## We CAN measure now, with the tools we actually have

| Capability | Where it comes from |
|---|---|
| Relative power / RMS / ADC sample statistics | `calibration_foundation._adc_statistics`, `rf_chain_characterization.analyze` (both reused, not reimplemented) |
| Clipping / rail-hit fraction | Same, formalized into `ClippingStatus` |
| Relative spectral shape (normalized bandpass) | `hi_spectral_metric.robust_psd_from_iq` + DC/spur masking already in `calibration_foundation.py` |
| Temporal stability (seconds/minutes/hours) | New — `calibration_engine.stability` |
| Gain-to-gain *relative* behavior (not absolute dB) | New analysis on top of the real historical gain-sweep methodology in `rf_gain_control_test.py`/`rf_chain_characterization.py` |
| Temperature correlation (DS18B20) | `temperature_sensors.DS18B20Reader` (reused as-is) + new correlation analysis |
| Frequency-bin consistency / persistent bad regions | New — built on the existing spur/DC detection |
| Session-to-session repeatability | New comparator, same spirit as `alignment_engine/hi/repeatability.py` |
| RFI occupancy/context | Existing `rfi_monitor.py`/`rfi_occupancy_map.py` outputs consulted, not recomputed |

## Naming rules (Fase 60)

Because a misleading name is worse than a missing feature, these names are
**forbidden** anywhere in `calibration_engine/` unless a real absolute
reference exists (it does not, in this pass):

`absolute_power_dbm`, `system_temperature_k`, `noise_figure_db`,
`sensitivity_jy`, `tsys_k`, `flux_jy`, `sefd`, `antenna_temperature_k`,
`calibrated_gain_db`.

Use instead: `relative_power_db`, `normalized_bandpass`,
`relative_gain_response`, `operational_noise_proxy`,
`relative_digital_power`, `relative_contrast`. A schema/lint test
(`test_calibration_engine_naming.py`) greps every module under
`calibration_engine/` for the forbidden list and fails the build if one
appears outside a comment/docstring explaining why it's forbidden.

## `CalibrationLevel`

```python
class CalibrationLevel(str, Enum):
    OPERATIONAL_RELATIVE = "OPERATIONAL_RELATIVE"     # everything this pass produces
    ABSOLUTE_RF_UNAVAILABLE = "ABSOLUTE_RF_UNAVAILABLE"  # explicit statement, not a future promise
```

There is no `ABSOLUTE_RF_CALIBRATED` value in this codebase yet. Adding one
is a deliberate future decision gated on real equipment (see
`CALIBRATION_MODEL.md`), not a value anyone should be able to reach by
accident.

## Deployment context (reused, not reinvented)

Calibration sessions reuse `alignment_engine.deployment_state.DeploymentState`
(`UNKNOWN`/`BENCH`/`INDOOR`/`FIELD`) purely as a **label**, not a movement
gate — Calibration V1 does no mount movement, so `check_hardware_movement_allowed`
is irrelevant here. What matters is that a session's deployment context is
recorded and BENCH/INDOOR results are never silently pooled with FIELD
results (Fase 5, Fase 68: INDOOR is valid for ADC/clipping/frequency-axis
work, not for sky bandpass/RFI-environment/antenna-response conclusions).
