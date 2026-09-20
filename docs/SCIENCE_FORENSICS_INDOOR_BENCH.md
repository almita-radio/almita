# SCIENCE FORENSICS INDOOR BENCH

Indoor bench is not astronomy. It proves **infrastructure**, not sky truth. Tool: `scripts/science_forensics_bench.py`
(additive; it does not modify `capture.py`, OBSERVE, REDUCE, SCIENCE, forensics V1, the plan or `observer_config.json`).

Current state: **READY FOR LIVE PREFLIGHT** (outdoor) is unchanged. The bench adds a separate ladder that never reaches READY FOR FIELD:
`READY FOR INDOOR BENCH PREFLIGHT` -> `INDOOR BENCH PASS` (results: `BENCH PASS` / `BENCH PARTIAL` / `BENCH BLOCKED`).

```
B="./.venv/bin/python scripts/science_forensics_bench.py"
```

## 1. Purpose

Use the time with the antenna and mount inside the apartment to prove everything that does not need the sky: Python environment, plan/CSV/observer_config
provenance, clocks and timestamp logic, storage, MAIN `rtl_tcp` reachable and accepting the plan's receiver settings, INDI reachable, the mount device present with
readable coordinates and tracking state, and **zero mount movement commands**.

## 2. What it proves

| check | how |
|---|---|
| repo/plan/CSV/observer_config integrity | HEAD and branch; frozen modules equal their pins; plan sha256 = `8bacb7ab79f99df2afc16213e7f3b26c2bdf832e07a04f06127ebb93cd301b4e`; CSV A/B/AB full sha256 from the plan; `observer_config.json` unmodified vs HEAD, and equal to the one the design used (a difference is reported: the plan assumes the designed site) |
| Python/venv, capture.py path, memory | modules importable; `MemAvailable`/`SwapFree` reported (Raspberry Pi: no concurrent heavy jobs) |
| clocks | UTC timezone-aware; monotonic clock non-decreasing and consistent with the wall clock; system clock not before the HEAD commit; local `timedatectl` state (no remote query) |
| timestamp pipeline | three synthetic captures A, B, A with **uneven actual timestamps**: bracketing recovers the injected 6919 Hz offset exactly; a planned even schedule would be off by 150 Hz |
| storage | real historical 10 s capture files (median 12.0 MB, compressed HDF5) and capture.py's uncompressed preflight bound (48 MB/capture x1.25): A 2.88 GB, B 3.12 GB, AB 5.04 GB vs 397 GB free |
| services and ports | read-only `systemctl is-active`; passive listening check on 1234 (MAIN), 1235 (RFI_REF), 7624 (INDI), 8090, 8088 from `/proc/net/tcp`; MAIN state `AVAILABLE` / `BUSY` / `UNKNOWN` (an established client on :1234 = BUSY, never touched) |
| MAIN SDR config | serial/gain/rate/port/`-T` (Bias-T) read from the `rtl_tcp.service` unit and compared with the plan (frequency: the unit starts at 1420405000, capture.py retunes to 1420405752 on every run) |
| INDI (live, read-only) | device name, CONNECTION, RA/Dec (validated), tracking state, pier side, park state, OnStep status, mount state; only `<getProperties/>` is sent |
| short SDR stream (live, `--yes`) | connect, plan settings through the same `SDRCapture` class capture.py uses, 0.2-5 s of bytes: samples received vs expected, RMS finite, clipping, constant blocks, receive gaps. Marked `BENCH DATA - NOT SCIENCE` |
| zero movement | see section 4 |

## 3. What it does NOT prove

The sky, the antenna pointing, visibility, RFI, the hydrogen line or any signal, the mount's mechanical freedom or slew behaviour, the outdoor site's
`observer_config.json`, REDUCE/SCIENCE/FORENSICS behaviour on real data, or that a **real preflight passes outdoors**. Indoor data carry no astronomical meaning:
the bench prints only "stream alive / RMS finite / no clipping". It never says hydrogen detected, line visible, RFI absent or sky valid. Nothing is passed to REDUCE.
Geometry windows are computed but marked `CALCULATED ONLY - NOT FIELD VALIDATED` and never block the bench.

## 4. Zero movement: enforced, traced, proven

1. **No movement API exists in the bench.** It does not import `capture.py` or `indi_telescope_control` (tested by AST). Its own INDI client can only send
   `<getProperties/>`: every other message (`new*Vector`, `CONNECTION`, park, sync, tracking, goto, abort, motion) is refused **before** it reaches the wire
   (`MovementBlocked`), and any movement verb passed to `mount_command` raises. The rtl_tcp socket is wrapped: only 0x01 frequency, 0x02 sample rate, 0x03 gain mode,
   0x04 gain are allowed; anything else (for instance Bias-T 0x0e) is blocked.
2. **Evidence from the wire.** The manifest's `movement_commands_sent` is computed from the messages actually sent (`only_getProperties_sent`), not declared.
3. **Trace mode** (`--trace`): every action as `READ`, `WRITE`, `CONNECT`, `CONFIGURE`, `BLOCKED_MOVEMENT`, `BLOCKED_WRITE`, e.g.
   `CONNECT INDI localhost:7624`, `READ INDI getProperties device=LX200 OnStep`, `CONNECT RTL MAIN localhost:1234`, `CONFIGURE RTL SET FREQUENCY`, and for the guard
   self-test `BLOCKED_MOVEMENT MOUNT GOTO -> BLOCKED`.
4. **INDOOR_MODE=1.** The bench sets it for its own process. Export it in your shell while the antenna is indoors: the field wrapper then refuses `preflight` and any
   live `run` (dry-run and `preflight --print-only` still work), so a preflight recorded indoors can never unlock READY FOR FIELD. Unset it outdoors.
5. **Bench output** only under `data/science_forensics_bench/<BENCH_ID>/` (never `data/mosaic`, `data/reduced`, campaign or REDUCE paths; enforced by a path guard and
   tested). It never writes the wrapper's `latest_preflight_*.json`.

## 5. Audit: is `capture.py --preflight-only` safe indoors?

`$B audit` (static, from the real source; capture.py is never executed): **SAFE INDOOR**.

- `main()` exits (`sys.exit`) at line 2561 before `execute_observation_plan` (line 2571) when `--preflight-only`, and `should_execute_after_preflight({'success': True}, True)` is `False`.
- No movement primitive (`goto`, `sync`, `set_tracking`, `park`, `unpark`, `ensure_tracking_off`, `confirm_tracking_on`, `execute_observation_plan`) is reachable from the code that runs before the gate plus
  `run_preflight`. All of them live inside `execute_observation_plan` (lines 1197-2305), which preflight never enters.
- **INDI:** `INDITelescopeControl.connect()` sends `getProperties` and, **only if the device is not yet CONNECTED**, a `CONNECTION` switch (`indi_telescope_control.py:247`; a driver connect, not a
  motion, park or tracking command). `get_coordinates()` sends only `getProperties`; `_read_indi_preflight_properties` runs `indi_getprop` (read). No tracking write, no park change, no mount config change.
- **SDR:** `SDRCapture.connect()` + `configure()` send only 0x01, 0x02, 0x03, 0x04 (receiver tuner) and read the dongle info/stream; no Bias-T command (Bias-T is the service's `-T` flag).
- Filesystem side effects only: a temporary HDF5 and a probe file are created and removed; `SessionManager()` initialises `data/IQ/session.csv` if missing.
- Caveats: preflight retunes MAIN rtl_tcp to the plan values; it may write `CONNECTION=On` once. The bench itself never runs capture.py, and the wrapper's `preflight` stays an **outdoor** step.
Negative controls are tested: a copy of capture.py with a `goto`/`ensure_tracking_off` inside `run_preflight`, the gate removed, a Bias-T command in `configure`, or a `new*Vector` in `get_coordinates` is judged `NOT SAFE INDOOR`.

## 6. Commands

```
$B status                      # offline + passive checks; writes nothing
$B audit                       # SAFE INDOOR / NOT SAFE INDOOR with file:line evidence
$B selftest                    # movement-guard and A-B-A timestamp self tests
$B preview                     # exact commands to use outside (printed, not executed)
$B dates --windows             # plan validity for today, contrast per date, start-window shift (about 20 s; CALCULATED ONLY)
$B dates --date 2026-11-01     # plan validity for a departure date
```
Live, one at a time (each states NO MOUNT MOVEMENT; `--trace` shows every action):
```
export INDOOR_MODE=1
$B check --live-readonly --trace                                   # INDI reads only; no confirmation needed
$B sdr --seconds 2 --yes --trace                                   # MAIN SDR short stream; BENCH DATA NOT SCIENCE; not saved
$B check --live-readonly --with-sdr --yes --seconds 2 --trace      # everything: BENCH PASS / INDOOR BENCH PASS
```
`--save-sample` (sdr/check) keeps the bytes as `data/science_forensics_bench/<id>/BENCH_NOT_SCIENCE_<id>.u8` with a sidecar banner; default is to keep nothing.
Live commands write `manifest.json` (bench_id, git_commit, branch, plan_sha256, observer_config_sha256, csv_sha256, start_utc, end_utc, rtl_tcp_reachable, indi_reachable,
mount_readable, `movement_commands_sent`, `science_capture`) and `report.json` there; `status` writes only with `--log`.
Result rules: **BLOCKED** = a hard failure (hash mismatch, frozen module changed, `observer_config.json` modified, clock insane, guard self-test failed, storage insufficient, INDI writes seen);
**PARTIAL** = no hard failure but something live was not executed or not available (SDR down, INDI unreadable, mount not readable); **PASS** = everything executed and sane.
RFI_REF (the second SDR, serial 00000002) is reported reachable/unavailable and never blocks.

## 7. Plan date validity and sensitivity (calculated, not field validated)

Contrast of the stationary-sky signature (channels) between A and B/C **at the same local sidereal time** on later dates (`$B dates --windows`):

| days after 2026-09-21 | 0 | 15 | 30 | 45 | 55 | 60 | 70 | 85 |
|---|---|---|---|---|---|---|---|---|
| B-A | +23.6 | +28.3 | +31.8 | +33.9 | +34.4 | +34.4 | +33.8 | +31.6 |
| C-A | -21.9 | -23.7 | -22.9 | -19.7 | -16.2 | -14.1 | -9.2 | -0.5 |

The contrast is not linear: C-A weakens by about 0.3 ch/day averaged over the first 90 days and faster later (about 0.5 ch/day around days 60-85), then falls below 75 % of the plan (`REGENERATE RECOMMENDED`)
around day 55 (mid November) and below 10 channels (`INVALID`) around day 70 (end of November). Today the plan is `VALID`. The start windows move **-3.93 min/day** (sidereal),
measured on the computed windows (e.g. B negative side 12:00 UTC on 2026-09-20 -> 11:05 UTC on 2026-10-04). The wrapper's `status` applies the same rules to the real date and time and
prints the command that writes a **new, separate** plan; the official plan is never modified.

## 8. Transition to the field

| step | required |
|---|---|
| 1 | `$B check --live-readonly --with-sdr --yes` -> `BENCH PASS` / `INDOOR BENCH PASS` |
| 2 | antenna deployed outdoors, mount mechanically free, clear movement path |
| 3 | `unset INDOOR_MODE`; INDI live and MAIN SDR live |
| 4 | `$S status B` (planned geometry window OK, epoch VALID or regenerate first) and `$S precheck B` |
| 5 | `$S preflight B` -> **PASS** (real `capture.py --preflight-only`) |
| => | `$S status B` says **READY FOR FIELD** (fresh, matching PASS only) |

Field day: do not regenerate anything by default. Run `status`, `precheck`, `preflight` first. If the epoch moved too far (status says `REGENERATE_RECOMMENDED` or `BLOCKED FOR PLANNED GEOMETRY`),
generate a **new, versioned** plan with the command `status` prints, then repeat the sequence with `--plan`. `$S` is `./.venv/bin/python scripts/science_forensics_field.py`
(see `docs/SCIENCE_FORENSICS_FIELD_RUNBOOK.md`).
