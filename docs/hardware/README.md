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

## Current hardware architecture

```text
                         ┌──────────────────┐
                         │  Raspberry Pi 5  │
                         │                  │
                         │  INDI / OnStep   │──────► Equatorial mount
                         │  rtl_tcp MAIN    │
                         │  rtl_tcp RFI_REF │
                         │  DS18B20         │
                         └──────┬─────┬─────┘
                                │     │
                 USB / MAIN SDR │     │ USB / RFI SDR
                                │     │
                                ▼     ▼
                     RTL-SDR Blog V4  RTL-SDR Blog V3
                                ▲     ▲
                                │     │
                    Hydrogen LNA│     │Wideband LNA
                                ▲     ▲
                                │     │
                       1420 MHz feed  FM notch filter
                                ▲     ▲
                                │     │
                    ~90 × 60 cm grid  Reference antenna
                         reflector
```

ALMITA deliberately uses replaceable modules rather than hiding the instrument
inside a sealed custom appliance. That makes field repair easier and keeps the
design approachable.

And yes, some mechanical integration still involves plastic cable ties.
They are not currently considered a calibrated structural element.
