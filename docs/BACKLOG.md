# ALMITA backlog

Operator requests and their real status, checked against the code and the commits on 2026-10-06.
"Verified" means automated tests (no hardware) and, where stated, a run on the real preserved session. Nothing
below was validated on the real mount or with a real capture unless it says so.

## Done and active (web-polish-v1)

| id | request | where | verification |
|---|---|---|---|
| BL-001 | CALIBRATE: HI Alt/Az map of the wizard plan | 156bfbd, 8a66860 | tests; real plan WIZARD-20261002 (preview az/alt = saved hi_plan; astropy recomputation agrees to 0.001°) |
| BL-002 | OBSERVE: calibration profile selector | 4085ed8, 915f8fa | tests; real profiles over HTTP (COMPATIBLE at 1420405752/2400000/40.2, rejected with reason otherwise; hashes unchanged) |
| BL-003 | Status indicator centred on every page | 03d263a | browser tests, 8 pages at 1280 / 390 px |
| BL-004 | No operational use of the retired :8090 | 704b168 | tests; blackbox no longer logs port_8090 |
| BL-005 | SCIENCE: A/B/C in one projection, full coverage, ≤3 GB | cf450de, 4eec7f4, 9c5c8f2 | real 400-point run SCIENCE_WEB-20261005-230737-640582: 400/400 used, quality GOOD, 394 s, measured peak RSS 1.03 GB; tiled = single cube to 1e-11 of the field scale on hostile synthetic inputs |
| BL-006 | SCIENCE: readable exports, uniform diagnostics, hatching explained | b40e126 | tests (identical image sizes, NOTES.md sections); not yet re-rendered on the real session |
| BL-007 | Reference Wizard: STOP / ABORT / failures / interrupted steps | adc2cac | tests (simulated 50 Ω backend, stubbed rtl_tcp, temp job dir, browser tests); **not exercised on real hardware** |
| BL-008 | Frequency: last operational flows on the central config + ACK evidence | aae14d9 | tests; forensics bench `sdr --yes` not run on hardware |
| BL-009 | Tests without data/ dependencies; no false capture.py conflict | fdc72e9, 8473f0c | full suite (2447 passed); the 31 failures of that run fixed and re-run green |
| BL-010 | Git: data/ generated and ignored, fixtures in tests/fixtures/, runbook versioned | ef32d62, dbafb43 | — |
| BL-011 | mount_control.py never awaited connect() | 5ee573a | fake-controller tests only; the tool was not run against the mount |

Activation notes: `console/` is served from the source tree (live on save); `almita-observe-api` must be
restarted for Python changes (web ops, wizard routing, catalog, SCIENCE bridge in-process imports);
`almita-console-watcher` for the capture.py detection change.

## Open — need the operator or a decision

- **CALIBRATE with the 50 Ω termination** (physical change at the LNA input) and a real wizard run: needed to
  validate BL-007 and to build a new profile. Not started by design.
- **Real-hardware checks** of BL-007 (STOP during a real capture), BL-008 (bench `sdr --yes` on MAIN) and
  BL-011 — none was run on the instrument.
- **Re-run SCIENCE on the 400-point session** to produce the BL-006 presentation on real data (≈7 min, CPU only;
  can be done from the web whenever convenient).
- **BL-002 optional**: upload a profile from the browser's disk (needs a decision: where uploads go and who may
  write under data/calibration). Not implemented.
- **No local horizon model**: the CALIBRATE map always says "OBSTACLES NOT EVALUATED" until one exists (needs site
  data).
- **Known open items** (FIELD_RUNBOOK.md §14): canonical session identity, INDI process lock, `gain='auto'`
  default in `sdr_capture.configure`, duplicate DS18B20 readers, outdated FLUJO_COMPLETO.md/README.md.
  `mount_control.py` CLI: a negative sexagesimal Dec (`-33:24:00`) is read by argparse as an option.
- **Untracked operator files** in the repo root (rf_chain_*, mount_slew_*, indoor_coupling_*, sun_detectability_*,
  `*_ultima_revision.tar.gz`, `examples/block-validation-01.yaml`, a stray file named `=.9`): left untouched -
  only the operator knows which are worth versioning.
- `test_field_console_left_open_for_30_minutes...` failed once inside the 30-minute full run and passes alone:
  timing-sensitive under load on the Pi.
