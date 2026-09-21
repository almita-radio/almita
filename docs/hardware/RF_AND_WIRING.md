# ALMITA RF and Wiring Topology

## MAIN science chain

```text
Sky
 │
 ▼
1420 MHz feed
 │  50 ohm RF
 ▼
Nooelec Hydrogen LNA
 │
 ▼
RTL-SDR Blog V4
 │  USB
 ▼
Raspberry Pi 5
 │
 ▼
rtl_tcp → OBSERVE → RAW HDF5
```

Nominal operating configuration:

- center frequency: **1420.405752 MHz**
- sample rate: **2.4 MS/s**
- receiver gain: **40.2 dB**
- Bias-T: **ON**
- science gain: fixed after preflight for the duration of a science session

The gain value is an experimentally characterized operating point, not a
universal constant for every possible ALMITA clone. A materially different RF
chain must repeat gain characterization.

## RFI_REF chain

```text
Local RF environment
 │
 ▼
Reference antenna
 │
 ▼
FM broadcast notch filter
 │
 ▼
Wideband LNA
 │
 ▼
Secondary RTL-SDR
 │  USB
 ▼
Raspberry Pi 5
 │
 ▼
rtl_tcp → RFI diagnostics
```

RFI_REF is intentionally independent from MAIN. It provides context and must
not become a dependency that can corrupt or block the primary science capture.

## Mount/control chain

```text
Raspberry Pi 5
 │
 ├─ INDI server
 │    └─ LX200 OnStep driver
 │          └─ OnStep controller
 │                └─ equatorial mount motors
 │
 └─ ALMITA observation / alignment / bench software
```

Normal science observation does not use arbitrary SYNC operations.

## Temperature telemetry

DS18B20 sensors are read over Linux 1-Wire/sysfs and are used for instrument
telemetry and thermal correlation. Temperature telemetry is deliberately kept
outside the SDR receive hot path.

## Grounding and field wiring

Keep RF leads short where practical, provide strain relief at SDR/LNA
connections, avoid routing motor/power leads tightly alongside the RF chain,
and preserve enough cable slack for the full intended mount motion.

Any field build should verify the complete mechanical travel before automated
movement.
