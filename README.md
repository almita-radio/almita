<p align="center">
  <img src="almita-logo.png" alt="ALMITA 21 cm hydrogen line radio telescope" width="100%">
</p>

# ALMITA

**Antenna Listening Mostly to Interference, Tentatively Astronomy**

ALMITA is a **fully open-source, open-hardware amateur radio astronomy project**: a **21 cm neutral hydrogen (HI) radio telescope** built in **Chile**
by **Felipe Fridman G.** It observes the **1420 MHz hydrogen line** with a Raspberry Pi 5 and RTL-SDR receivers.
Contact: **ffridman@gmail.com**

It combines INDI/OnStep mount control, automated sky planning, HDF5 acquisition and a lightweight local web console. It is
**offline-first**: internet access is not required to observe.

Project site: <https://almita-radio.github.io/almita/>

ALMITA is mostly vibe-coded, extensively field-tested, occasionally threatened with a hammer, and still held together in
suspiciously many places by **plastic cable ties**. This is considered temporary. It has also been considered temporary for
quite some time.

## What it measures (and what it does not)

ALMITA records raw I/Q from an RTL-SDR while an equatorial mount steps through a planned sky mosaic, and turns it into
spectra, velocity cubes and maps.

**All HI products are relative / instrumental.** There is no absolute calibration in Kelvin (or any physical unit) yet; that is a
development goal, not a capability we pretend to have. The antenna beam used for mapping is an **operator-supplied, provisional
operational model** until a proper beam-characterisation campaign exists. Nothing in the pipeline interprets or classifies
astrophysical signals automatically.

## Pipeline

```text
OBSERVE  ->  REDUCE V1 (frozen)  ->  SCIENCE V1 (frozen)
raw I/Q      reduced spectra          beam-aware spatial gridding,
+ metadata   (masks, relative         LSRK velocity cube, relative integrated
(HDF5)       intensity, LSRK          maps, uncertainty / coverage maps
             velocity, uncertainty)
```

Supporting systems: **ALIGN** (conservative pointing alignment), **CALIBRATE** (receiver / gain characterisation) and
**RFI_REF** (an independent second SDR chain that watches the local RF environment without ever interfering with the main capture).

| Command (run from the repo root) | Purpose |
|---|---|
| `almita_observe.py` | plan and run observations |
| `almita_align.py` / `almita_calibrate.py` | alignment and calibration workflows |
| `almita_reduce.py` | RAW -> reduced spectra |
| `almita_science.py` | reduced spectra -> science products (`inspect`, `plan`, `run`, `replay`, `compare`, `validate`) |
| `almita_console_server.py`, `almita_orchestrator_server.py` | local web console (:8088) and observe API (:8090) |

Every command has `--help`. The long-running services are described under `systemd/`.

## Hardware (short)

Raspberry Pi 5 - RTL-SDR Blog V4 (science) and V3 (RFI reference) - Nooelec Hydrogen LNA - 1420 MHz feed on a modified ~90 x 60 cm
grid reflector - equatorial mount with OnStep + INDI - temperature sensors. Intentionally accessible, experimental and repairable;
not a commercial kit. ALMITA-original hardware design and integration documentation are published under **CERN-OHL-S-2.0**; software is **MIT licensed**. Commercial modules remain under their manufacturers’ terms. Details: [docs/hardware/](docs/hardware/) and [docs/ALMITA_OVERVIEW.md](docs/ALMITA_OVERVIEW.md).

## Documentation

| Topic | Where |
|---|---|
| Project overview (hardware, workflow, grid generator, alignment, gain, RFI reference) | [docs/ALMITA_OVERVIEW.md](docs/ALMITA_OVERVIEW.md) |
| Observation planning / grids | [docs/observe/](docs/observe/) |
| ALIGN | [docs/HI_ALIGNMENT_MODEL.md](docs/HI_ALIGNMENT_MODEL.md), [docs/HI_FIRST_LIGHT_RUNBOOK.md](docs/HI_FIRST_LIGHT_RUNBOOK.md) |
| CALIBRATE | [docs/CALIBRATION_SCOPE.md](docs/CALIBRATION_SCOPE.md), [docs/CALIBRATION_MODEL.md](docs/CALIBRATION_MODEL.md), [docs/CALIBRATION_FIRST_TEST_RUNBOOK.md](docs/CALIBRATION_FIRST_TEST_RUNBOOK.md) |
| REDUCE | [docs/REDUCE_SCOPE.md](docs/REDUCE_SCOPE.md), [docs/REDUCE_MODEL.md](docs/REDUCE_MODEL.md), [docs/REDUCE_PIPELINE.md](docs/REDUCE_PIPELINE.md), [docs/REDUCE_V1_FREEZE.md](docs/REDUCE_V1_FREEZE.md), [runbook](docs/REDUCE_FIRST_RUNBOOK.md) |
| SCIENCE | [docs/SCIENCE_SCOPE.md](docs/SCIENCE_SCOPE.md), [docs/SCIENCE_MODEL.md](docs/SCIENCE_MODEL.md), [docs/SCIENCE_PIPELINE.md](docs/SCIENCE_PIPELINE.md), [docs/SCIENCE_ACCEPTANCE.md](docs/SCIENCE_ACCEPTANCE.md), [docs/SCIENCE_V1_FREEZE.md](docs/SCIENCE_V1_FREEZE.md), [runbook](docs/SCIENCE_FIRST_RUNBOOK.md) |
| Web console | [docs/WEB_ALIGNMENT_CALIBRATION.md](docs/WEB_ALIGNMENT_CALIBRATION.md) |
| Hardware notes (mount model, rtl_tcp service) | [docs/hardware/](docs/hardware/) |
| Full documentation index | [docs/README.md](docs/README.md) |

## Status and installation

Experimental and field-tested on one Raspberry Pi 5. ALMITA is **not packaged**: it runs from a checkout under a Python
virtual environment (`requirements.txt`), with the services in `systemd/` installed by hand. There is no `pip install`.

Tests: from the repo root, `python -m pytest` (test files are in [tests/](tests/)). Some tests need optional packages (for example `indi`) or real
historical campaigns under `data/`, and fail or skip without them.
