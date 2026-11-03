# ALMITA Open Hardware

ALMITA is an **open-hardware radio telescope design** built from accessible
commercial modules plus openly documented integration, RF topology, mounting
and wiring.

The ALMITA-original hardware documentation in this directory is licensed under
**CERN-OHL-S-2.0**. See [../../LICENSE.hardware](../../LICENSE.hardware).

Commercial off-the-shelf parts remain under their manufacturers' own terms.
Open hardware here means that ALMITA's **design, integration and modifications
are published so the instrument can be studied, reproduced, modified and
shared**.

## Build documents

- [BUILD.md](BUILD.md) — system-level build and bring-up
- [BOM.md](BOM.md) — bill of materials
- [RF_AND_WIRING.md](RF_AND_WIRING.md) — RF, USB, control and sensor topology
- [MECHANICAL.md](MECHANICAL.md) — reflector, feed and mount integration
- [MOUNT_SLEW_MODEL.md](MOUNT_SLEW_MODEL.md) — measured mount behaviour
- [RTL_TCP_SYSTEMD.md](RTL_TCP_SYSTEMD.md) — SDR service integration

## Current hardware architecture (November 2026)

```text
  ┌──────────────────────────────┐   Ethernet (10 m UTP)   ┌──────────────────────────────┐
  │ Notebook                     │◄──────────────────────► │ Raspberry Pi 4 — capture box │
  │  web console :8088 (login)   │  rtl_tcp :1234 · INDI   │  (closed metal box)          │
  │  capture / quick-look / RFI  │  :7624 · agent :8099    │  rtl_tcp MAIN                │
  │  masks / REDUCE / SCIENCE    │                         │  INDI via KStars/Ekos ───────┼──► OnStep ──► equatorial mount
  └──────────────────────────────┘                         │  instrument agent (read-only)│
                                                           └──────────────┬───────────────┘
                                                                          │ USB
                                                                          ▼
                                                                  RTL-SDR Blog V3 (MAIN)
                                                                          ▲  coax (Bias-T powers the LNA)
                                                                  Nooelec SAWbird+ H1 LNA
                                                                          ▲  ~11 cm pigtail
                                                       DIY dipole feed v1 + 12 cm reflector plate
                                                                          ▲
                                                             ~90 × 60 cm grid dish
```

Until November 2026 a single Raspberry Pi 5 did everything (and the software still supports that). The Pi was moved away from
the feed into a metal box because it radiates a 357 kHz comb and narrow lines into the 1420 MHz band. The second chain
(RFI_REF: reference antenna → FM notch → wideband LNA → RTL-SDR) is supported but currently disconnected, and the DS18B20
thermometers are off while their harness on the GPIO is an RF source.

ALMITA deliberately uses replaceable modules rather than hiding the instrument
inside a sealed custom appliance. That makes field repair easier and keeps the
design approachable.

And yes, some mechanical integration still involves plastic cable ties.
They are not currently considered a calibrated structural element.
