<p align="center">
  <img src="almita-logo.png" alt="ALMITA 21 cm hydrogen line radio telescope" width="100%">
</p>

# ALMITA

**Antenna Listening Mostly to Interference, Tentatively Astronomy**

ALMITA is a **fully open-source, open-hardware amateur radio astronomy project**: a **21 cm neutral hydrogen (HI) radio telescope** built in **Chile**
by **Felipe Fridman G.** It observes the **1420 MHz hydrogen line** with RTL-SDR receivers, a modified grid dish and an
OnStep equatorial mount. A notebook runs the web console and the processing, and a Raspberry Pi in a shielded box sits at the instrument.
Contact: **ffridman@gmail.com**

It combines INDI/OnStep mount control, automated sky planning, HDF5 acquisition, live RFI masking and a local web console
behind a login. It is **offline-first**: internet access is not required to observe.

Project site: <https://almita-radio.github.io/almita/>

ALMITA is mostly vibe-coded, extensively field-tested, occasionally threatened with a hammer, and still held together in
suspiciously many places by **plastic cable ties**. This is considered temporary. It has also been considered temporary for
quite some time.

> **Where the newest code is.** `main` holds the documentation, the licenses and the frozen REDUCE/SCIENCE V1 core. The web
> console and everything listed under [What is new](#what-is-new-up-to-november-2026) are developed on the branch
> [`rfi-masks-v1`](https://github.com/almita-radio/almita/tree/rfi-masks-v1), which builds on `web-polish-v1`. The merge into `main`
> is pending ([PR #7](https://github.com/almita-radio/almita/pull/7) is the first step).

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
| `almita_orchestrator_server.py` | the web console and its API on one port (:8088, login) |
| `transit_logger.py`, `lobe_transit.py` | solar transit: still-antenna logger / beam fit with the mount |
| `rfi_mask.py`, `rfi_mask_live.py`, `local_rfi_check.py` | RFI masks of every capture, live; quick local RFI check |

Every command has `--help`. The long-running services are described under `systemd/`.

## What is new (up to November 2026)

**Two machines instead of one.** The instrument used to run entirely on a Raspberry Pi 5. In November 2026 it was split:
- **Capture Pi:** a Raspberry Pi 4 inside a metal box at the dish. It runs `rtl_tcp`, INDI (started from KStars/Ekos on its desktop) and a small
  read-only **instrument agent** (`instrument_agent.py`, port 8099). The agent reports the SDR service, kernel messages, the SoC temperature and the
  USB devices, and nothing else. The CPU clock is fixed at boot.
- **Notebook:** runs the web console, captures, quick-look and processing as systemd *user* units (`systemd/notebook/`, `scripts/install_notebook.sh`).
  `instrument_forwarder` makes the capture Pi's SDR and INDI ports look local. The single-machine setup still works.

**Web console** (one port, `:8088`, with a login; DÍA/NOCHE theme for sunlight; LX200 / SIMULATOR mount switch and a **STOP MOUNT** button on every page):

| Page | What it does |
|---|---|
| STATUS | instrument health, temperatures, LNA (Bias-T) power, a quick **local RFI check** of MAIN |
| PIPELINE | the whole chain end to end: preflight → align → calibrate → observe → reduce → science |
| LOBE | beam measurement from a solar transit (drift or RA scan) with a live Gaussian fit |
| **TRANSIT** | the newest page: a band-power logger every 10 s with the antenna still, START / STOP / CLEAR, last 30 / 60 / 180 min with the capture Pi's temperature |
| ALIGN | conservative pointing alignment, guarded SYNC on the Sun or on HI, a SUN SYNC button for a hand-pointed dish |
| CALIBRATE | the calibration wizard (receiver/gain profile with a 50 Ω load) and HI reference zones |
| OBSERVE | plan and run a sky mosaic: PREFLIGHT, sky map of the grid, coverage drawn with the real 20° beam, auto meridian flip |
| MONITOR | live spectra, 3D spectral stack, **RFI MASK** panel and the *Unknown Pleasures* art view of the session |
| REDUCE | REDUCE V1 per capture or per campaign, with an optional **RFI mask** |
| SCIENCE | SCIENCE V1 cubes and maps |
| SESSIONS | catalog of every session with a sky map of the real pointings, filters and safe deletion |
| DESKTOP | the capture Pi's desktop (noVNC) behind the same login, so KStars can be seen remotely |

**Screenshots of every page:** [docs/WEB_CONSOLE_SCREENSHOTS.md](docs/WEB_CONSOLE_SCREENSHOTS.md).

<p align="center">
  <img src="docs/screenshots/observe.jpg" alt="OBSERVE page: the planned region over the HI4PI sky" width="32%">
  <img src="docs/screenshots/monitor.jpg" alt="MONITOR page" width="32%">
  <img src="docs/screenshots/transit.jpg" alt="TRANSIT page" width="32%">
</p>

**RFI work.**
- The local interference was hunted down. A 357 kHz comb and a forest of narrow lines turned out to be radiated by the Raspberry Pi itself and by the
  thermometer harness on its GPIO.
- Moving the Pi away from the feed into a closed metal box made the comb disappear.
- `rfi_mask.py` / `rfi_mask_live.py` now flag comb teeth, narrow lines and time pulses in every capture as it is recorded.
- REDUCE can drop those frames and flag those channels (opt-in; the frozen REDUCE engine itself is unchanged).

**Feed and receiver.**
- MAIN is now an RTL-SDR Blog V3 (the V4 was retired) with a Nooelec SAWbird+ H1 LNA.
- A Y-factor of only ~1 dB on 2026-10-18 led to a new DIY feed: a copper-strip dipole in a 3D-printed case, modelled with NEC2.
- A 12 cm reflector plate is being fitted, with the dipole about 24 cm from the dish vertex and parallel to the grid bars.

**Still pending.** The new chain has **not yet detected HI**. Results stay relative and instrumental, the beam is still provisional, and the next steps are
a solar transit (TRANSIT / LOBE), a new calibration profile with the boxed capture Pi, and outings under open sky.

## Hardware (short)

Notebook (web + processing) and a Raspberry Pi 4 capture box (`rtl_tcp`, INDI) - RTL-SDR Blog V3 (MAIN) - Nooelec SAWbird+ H1 LNA -
DIY 1420 MHz dipole feed with a reflector plate on a modified ~90 × 60 cm grid dish - equatorial mount with OnStep + INDI. A second, independent
RFI_REF chain (RTL-SDR + FM notch + wideband LNA) is supported by the software but currently disconnected. The DS18B20 thermometers are off
while their harness is an RF source. Intentionally accessible, experimental and repairable; not a commercial kit.
ALMITA-original hardware design and integration documentation are published under **CERN-OHL-S-2.0**; software is **MIT licensed**.
Commercial modules remain under their manufacturers' terms. Details: [docs/hardware/](docs/hardware/) and [docs/ALMITA_OVERVIEW.md](docs/ALMITA_OVERVIEW.md).

## Documentation

| Topic | Where |
|---|---|
| Project overview (hardware, workflow, grid generator, alignment, gain, RFI reference) | [docs/ALMITA_OVERVIEW.md](docs/ALMITA_OVERVIEW.md) |
| Observation planning / grids | [docs/observe/](docs/observe/) |
| ALIGN | [docs/HI_ALIGNMENT_MODEL.md](docs/HI_ALIGNMENT_MODEL.md), [docs/HI_FIRST_LIGHT_RUNBOOK.md](docs/HI_FIRST_LIGHT_RUNBOOK.md) |
| CALIBRATE | [docs/CALIBRATION_SCOPE.md](docs/CALIBRATION_SCOPE.md), [docs/CALIBRATION_MODEL.md](docs/CALIBRATION_MODEL.md), [docs/CALIBRATION_FIRST_TEST_RUNBOOK.md](docs/CALIBRATION_FIRST_TEST_RUNBOOK.md) |
| REDUCE | [docs/REDUCE_SCOPE.md](docs/REDUCE_SCOPE.md), [docs/REDUCE_MODEL.md](docs/REDUCE_MODEL.md), [docs/REDUCE_PIPELINE.md](docs/REDUCE_PIPELINE.md), [docs/REDUCE_V1_FREEZE.md](docs/REDUCE_V1_FREEZE.md), [runbook](docs/REDUCE_FIRST_RUNBOOK.md) |
| SCIENCE | [docs/SCIENCE_SCOPE.md](docs/SCIENCE_SCOPE.md), [docs/SCIENCE_MODEL.md](docs/SCIENCE_MODEL.md), [docs/SCIENCE_PIPELINE.md](docs/SCIENCE_PIPELINE.md), [docs/SCIENCE_ACCEPTANCE.md](docs/SCIENCE_ACCEPTANCE.md), [docs/SCIENCE_V1_FREEZE.md](docs/SCIENCE_V1_FREEZE.md), [runbook](docs/SCIENCE_FIRST_RUNBOOK.md) |
| Web console | [screenshots of every page](docs/WEB_CONSOLE_SCREENSHOTS.md), [docs/WEB_ALIGNMENT_CALIBRATION.md](docs/WEB_ALIGNMENT_CALIBRATION.md); the current twelve-page console: [WEB_ARCHITECTURE](https://github.com/almita-radio/almita/blob/rfi-masks-v1/docs/WEB_ARCHITECTURE.md), [WEB_OPERATIONS](https://github.com/almita-radio/almita/blob/rfi-masks-v1/docs/WEB_OPERATIONS.md) (development branch) |
| Hardware (architecture, build, BOM, RF and wiring, DIY feed, mount model, rtl_tcp service) | [docs/hardware/](docs/hardware/) |
| Full documentation index | [docs/README.md](docs/README.md) |

## Status and installation

Experimental and field-tested, first on one Raspberry Pi 5 and since November 2026 on a notebook plus a Raspberry Pi 4 capture box.
ALMITA is **not packaged**: it runs from a checkout under a Python virtual environment (`requirements.txt`). The services are in `systemd/`;
on the development branch `scripts/install_notebook.sh` and `scripts/install_capture_pi.sh` install them. There is no `pip install`.

Tests: from the repo root, `python -m pytest` (test files are in [tests/](tests/)). Some tests need optional packages (for example `indi`) or real
historical campaigns under `data/`, and fail or skip without them.
