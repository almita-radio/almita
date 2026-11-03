# ALMITA Bill of Materials

This is the current system-level BOM for the ALMITA 21 cm instrument. Exact
vendor revisions may change; the functional role and interface are what matter
for reproducibility.

| Subsystem | Current part / implementation | Function |
|---|---|---|
| Compute (console) | Notebook (Ubuntu) | Web console, captures, quick-look, RFI masks, REDUCE / SCIENCE |
| Compute (instrument) | Raspberry Pi 4 in a closed metal box | `rtl_tcp`, INDI (KStars/Ekos), read-only instrument agent; fixed CPU clock |
| Compute (earlier) | Raspberry Pi 5 | Single-machine setup used until November 2026; still supported |
| MAIN receiver | RTL-SDR Blog V3 | Primary 21 cm I/Q acquisition (the V4 used before was retired in October 2026) |
| MAIN LNA | Nooelec SAWbird+ H1 | 1420 MHz LNA + SAW filter, powered over the SDR's Bias-T |
| MAIN antenna | DIY dipole feed v1: copper-strip dipole in a 3D-printed case, ~11 cm RG-58 pigtail, 12 cm reflector plate (being fitted) | Illuminates the grid reflector (modelled with NEC2; elements trimmed on a NanoVNA) |
| Reflector | Modified ~90 × 60 cm grid/parabolic reflector | Primary collecting aperture |
| RFI receiver | RTL-SDR (second unit) | Independent local RF reference — currently disconnected |
| RFI filter | Nooelec Flamingo FM | FM broadcast rejection in reference chain |
| RFI LNA | RTL-SDR.com Wideband LNA | Reference-chain amplification |
| RFI antenna | Independent reference antenna | Local RF environment sampling |
| Mount | Equatorial mount | Two-axis pointing |
| Mount controller | OnStep | Motor control and telescope protocol |
| Mount interface | INDI / LX200 OnStep driver | Software control and telemetry |
| Temperature | DS18B20 sensors; the capture Pi's SoC sensor | SDR/LNA/load thermal telemetry (DS18B20 currently off: their harness radiated into the feed) |
| RFI hygiene | Clip-on ferrites on USB/Ethernet/power leads; metal box for the capture Pi | Keep the computers' own emissions out of the band |
| Test equipment | NanoVNA, 50 Ω load | Feed tuning, cable checks, calibration reference |
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
