# ALMITA RF and Wiring Topology

## MAIN science chain

```text
Sky
 │
 ▼
~90 × 60 cm grid dish
 │
 ▼
DIY dipole feed v1 (copper strip, parallel to the grid bars, ~24 cm from the vertex)
 │  ~11 cm RG-58 pigtail, SMA
 ▼
Nooelec SAWbird+ H1 LNA (outside the feed case, so a 50 Ω load can be swapped in)
 │  coax, LNA powered by the SDR's Bias-T
 ▼
RTL-SDR Blog V3
 │  USB (ferrites)
 ▼
Raspberry Pi 4 in a closed metal box
 │  rtl_tcp :1234 over Ethernet
 ▼
Notebook: capture → RAW HDF5, live RFI masks, quick-look
```

Until November 2026 the SDR hung directly from a Raspberry Pi 5 that also ran everything else; that setup is still supported.

Nominal operating configuration:

- center frequency: **1420.405752 MHz**
- sample rate: **2.4 MS/s**
- receiver gain: **40.2 dB**
- Bias-T: **ON**
- science gain: fixed after preflight for the duration of a science session

The gain value is an experimentally characterized operating point, not a
universal constant for every possible ALMITA clone. A materially different RF
chain must repeat gain characterization.

## RFI_REF chain (currently disconnected)

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
host computer
 │
 ▼
rtl_tcp → RFI diagnostics
```

RFI_REF is intentionally independent from MAIN. It provides context and must
not become a dependency that can corrupt or block the primary science capture.

## Mount/control chain

```text
Raspberry Pi 4 (capture box)
 │
 ├─ KStars / Ekos → INDI server (:7624)
 │    └─ LX200 OnStep driver (USB serial)
 │          └─ OnStep controller
 │                └─ equatorial mount motors
 │
 └─ instrument agent (:8099, read-only)

Notebook
 └─ ALMITA observation / alignment / transit software, reaching INDI and rtl_tcp through instrument_forwarder
```

Normal science observation does not use arbitrary SYNC operations.

## Temperature telemetry

DS18B20 sensors are read over Linux 1-Wire/sysfs and are used for instrument
telemetry and thermal correlation. Temperature telemetry is deliberately kept
outside the SDR receive hot path.

In October 2026 the DS18B20 harness on the Pi's GPIO was found to radiate a 357 kHz comb and a forest of narrow lines into
the feed; a ferrite on it reduced them, and later tests showed that the Pi also radiates the comb through the air by itself. The sensors are off for now, and the capture Pi reports its own SoC temperature
instead (the TRANSIT page plots it).

## Computers as RF sources

The computers themselves are the worst local interferers measured so far. Keep them metres away from the feed and inside a
closed metal box, with ferrites on USB, Ethernet and power leads, and check with the local RFI check before every outing.
A person standing next to the Pi or the feed also brings interference back.

## Grounding and field wiring

Keep RF leads short where practical, provide strain relief at SDR/LNA
connections, avoid routing motor/power leads tightly alongside the RF chain,
and preserve enough cable slack for the full intended mount motion.

Any field build should verify the complete mechanical travel before automated
movement.
