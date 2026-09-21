# Building an ALMITA-class Instrument

This document describes the reproducible system architecture of the current
ALMITA hardware.

## 1. Assemble the antenna

Mount the 1420 MHz feed at the focus of the approximately 90 × 60 cm grid
reflector. The feed must be mechanically stable and connected to the MAIN RF
chain with 50 ohm coax.

Attach the reflector/feed assembly to an equatorial mount with enough cable
slack for the complete approved motion envelope.

## 2. Assemble MAIN

Connect:

```text
1420 MHz feed → Hydrogen LNA → RTL-SDR Blog V4 → Raspberry Pi 5
```

The current operational receiver settings are 1420.405752 MHz, 2.4 MS/s and
40.2 dB gain with Bias-T enabled.

Do not assume 40.2 dB is correct for a materially different RF chain; run the
gain-characterization workflow first.

## 3. Assemble RFI_REF

Connect the independent reference path:

```text
reference antenna → FM notch → wideband LNA → secondary RTL-SDR → Raspberry Pi 5
```

The RFI reference is contextual. MAIN remains the science receiver.

## 4. Connect mount control

Install/configure OnStep on the equatorial mount and expose it through an INDI
server using the LX200 OnStep driver.

Verify read-only state first: coordinates, tracking, park and controller
status. Only after mechanical clearance is confirmed should movement tests be
performed.

## 5. Add temperature telemetry

Attach DS18B20 sensors to the receiver/LNA locations of interest and expose
them through Linux 1-Wire/sysfs.

Sensor identity-to-location mapping belongs in the ALMITA configuration.

## 6. Install software

Clone the repository, create the Python virtual environment, install
`requirements.txt`, install the required INDI/rtl-sdr system packages, then
install the provided systemd services as appropriate for the host.

See:

- [RTL_TCP_SYSTEMD.md](RTL_TCP_SYSTEMD.md)
- [../ALMITA_OVERVIEW.md](../ALMITA_OVERVIEW.md)
- [../CALIBRATION_SCOPE.md](../CALIBRATION_SCOPE.md)
- [../REDUCE_SCOPE.md](../REDUCE_SCOPE.md)
- [../SCIENCE_SCOPE.md](../SCIENCE_SCOPE.md)

## 7. Characterize before observing

A newly built or materially modified ALMITA should not jump directly to a
science map.

Validate, in this order:

1. passive hardware/service state;
2. SDR sustained streaming and clipping/headroom;
3. receiver gain characterization;
4. mount read-only telemetry;
5. small controlled mount movement;
6. alignment / pointing procedure;
7. RF preflight;
8. short real-sky capture;
9. REDUCE validation;
10. SCIENCE products.

## 8. Preserve provenance

Record the actual parts, physical changes, configuration and characterization
results for each realization.

ALMITA is open hardware, but open hardware does not make two physically
different antennas magically identical.
