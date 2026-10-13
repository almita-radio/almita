# ALMITA Bill of Materials

This is the current system-level BOM for the ALMITA 21 cm instrument. Exact
vendor revisions may change; the functional role and interface are what matter
for reproducibility.

| Subsystem | Current part / implementation | Function |
|---|---|---|
| Compute | Raspberry Pi 5 | Local control, acquisition, processing and web UI |
| MAIN receiver | RTL-SDR Blog V4 | Primary 21 cm I/Q acquisition |
| MAIN LNA | Nooelec Hydrogen LNA | 1420 MHz science-chain amplification |
| MAIN antenna | 1420 MHz feed | Illuminates the grid reflector |
| Reflector | Modified ~90 × 60 cm grid/parabolic reflector | Primary collecting aperture |
| RFI receiver | RTL-SDR Blog V3 | Independent local RF reference |
| RFI filter | Nooelec Flamingo FM | FM broadcast rejection in reference chain |
| RFI LNA | RTL-SDR.com Wideband LNA | Reference-chain amplification |
| RFI antenna | Independent reference antenna | Local RF environment sampling |
| Mount | Equatorial mount | Two-axis pointing |
| Mount controller | OnStep | Motor control and telescope protocol |
| Mount interface | INDI / LX200 OnStep driver | Software control and telemetry |
| Temperature | DS18B20 sensors | SDR/LNA thermal telemetry |
| Interconnect | 50 ohm coax + SMA-family RF interconnects | RF signal path |
| Mechanical | Feed/reflector/mount brackets and ordinary field hardware | Physical integration |

## Design substitutions

The project is intentionally modular.

A substitute is acceptable when it preserves the relevant interface and
performance requirement, for example:

- a compatible RTL-SDR-class receiver with stable 2.4 MS/s streaming;
- a low-noise 1420 MHz amplifier with suitable gain and bias arrangement;
- a mechanically equivalent reflector/feed arrangement;
- an equatorial mount supported by INDI and capable of repeatable small slews.

Any substitution that changes gain, bandpass, noise behaviour, beam shape or
pointing behaviour must be re-characterized before being treated as equivalent.

## What is ALMITA-original

ALMITA's original contribution is primarily the **system integration**:
topology, software-controlled observing workflow, RF operating-point
characterization, calibration model, data/provenance model, mount/receiver
coordination, safety gates and reproducible processing chain.

Third-party modules remain third-party hardware.
