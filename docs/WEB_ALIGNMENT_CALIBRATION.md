# ALMITA Web: ALIGN + CALIBRATE

Extends the existing `:8090` app (`almita_orchestrator_server.py`, already
OBSERVE's home) with two new workflows, additively - OBSERVE's own routes,
files, and behavior are unchanged. `:8088` (Field Console) is untouched.

## Architecture

**The web is not the instrument.** Every number shown anywhere in ALIGN or
CALIBRATE comes from `alignment_engine`/`calibration_engine`, called
either directly or through `almita_align.py`/`almita_calibrate.py`'s own
CLI functions (never reimplemented in the web layer, never computed in
JavaScript).

```
almita_orchestrator_server.py   ThreadingHTTPServer, routes for OBSERVE (unchanged) + ALIGN + CALIBRATE
  |-- almita_web_align.py       ALIGN handlers -> almita_align.py (_engine_for, cmd_plan, cmd_preflight, cmd_run)
  |-- almita_web_calibrate.py   CALIBRATE handlers -> almita_calibrate.py (cmd_plan, cmd_preflight, cmd_run) + calibration_engine
  \-- almita_web_common.py      envelope(), SDRResourceStatus, session listing, path-traversal guard, JobRegistry
console/
  align.html + align.js         ALIGN page
  calibrate.html + calibrate.js CALIBRATE page
  styles.css                    shared with OBSERVE, extended additively
```

### Audit finding that shaped this design

`almita_orchestrator_server.py` is **not** async - it is a plain
`http.server.ThreadingHTTPServer` (one OS thread per request), no
event loop lives across requests. Calling `asyncio.run()` inside a
request handler is therefore safe here (each thread gets its own fresh
loop, nothing to nest inside) - this is different from the "never
`asyncio.run()` inside an endpoint" rule that applies to a real async
framework, and is used deliberately for `preflight` (the one genuinely
async call). Long operations (`run`) are handed to a background
`threading.Thread` instead, with the HTTP response returning immediately
- the frontend polls `GET .../session/{id}` (Fase 30), reusing the
engines' own session evidence as the job's status, rather than building a
separate job-tracking layer.

## Routes

Static: `/align.html`, `/align.js`, `/calibrate.html`, `/calibrate.js`
(added to `STATIC_FILES`, same read-only serving path as `/observe.html`).

```
GET  /api/align/status
GET  /api/align/session/{id}
POST /api/align/plan/{mode}          mode = solar | hi
POST /api/align/preflight/{mode}
POST /api/align/run/{mode}           background job, --simulate only
POST /api/align/replay               HI only
POST /api/align/compare              HI only
POST /api/align/sync/prepare/{mode}
POST /api/align/sync/apply/{mode}

GET  /api/calibrate/status
GET  /api/calibrate/session/{id}
POST /api/calibrate/plan
POST /api/calibrate/preflight
POST /api/calibrate/run              background job, --backend simulated only
POST /api/calibrate/replay
POST /api/calibrate/compare
POST /api/calibrate/profile/build
```

Response envelope for all NEW routes (OBSERVE's own routes are
unchanged): `{"ok": bool, "blocked": bool, "reason": str|null, "data": ...}`.
HTTP status is 200 for both ok and blocked results - **BLOCKED is a
first-class state** (Fase 33), not an HTTP error; only malformed
requests/unknown routes/server exceptions return 400/404/500.

## Safety

- **Sync**: `/api/align/sync/{prepare,apply}` are unconditionally blocked
  for both modes today - HI by `alignment_phase=FIRST_LIGHT_HI`
  (`sync_policy.evaluate_phase_gate`), Solar because no real-SYNC
  authorization exists anywhere in this codebase. There is no code path
  that can flip either.
- **Real hardware**: `RUN REAL` exists in the ALIGN UI but is never
  enabled by this iteration (deployment is not FIELD); CALIBRATE's `RUN
  REAL` is present but its endpoint is not wired to hardware at all in
  this pass (real capture stays a separate script,
  `calibration_operational_realtest.py`, run manually with full precheck
  - see its own docs).
- **Resource ownership** (Fase 46): `SDRResourceStatus` in
  `almita_web_common.py` checks, read-only: `capture_conflict`, the
  orchestrator's `orchestrator_state`, and (new) a `quicklook_live.py`
  process scan - the exact gap the calibration incident exposed. Reused
  by both ALIGN and CALIBRATE status endpoints. Never signals or kills
  anything it finds.
- **Path traversal** (Fase 57): every endpoint that accepts a session id
  or directory path (`session_id`, `session_dir`, `analysis_dir_a/b`,
  `session_a/b`) is resolved against its session root and rejected if it
  does not land inside it - `resolve_within_root()` in
  `almita_web_common.py`. Found and fixed during this pass: a literal
  `".."` session id, and an absolute path, both bypassed a naive
  `is_dir()`-only check and could have let `CalibrationSession`/
  `AlignmentSession`'s own `mkdir(parents=True, exist_ok=True)` create
  directories outside the intended root. Covered by
  `test_almita_web_align_calibrate.py`.
- **Terminology**: `/api/calibrate/status` distinguishes
  `VERIFIED_BY_DEVICE_READBACK` (the rtl_tcp connection handshake),
  `VERIFIED_BY_SERVICE_COMMAND_LINE` (the live process's own `ps` argv),
  and `CONFIGURED_EXPECTED` (a fallback default) - the frontend renders a
  tag per field and never claims a configured value was read from the
  device.

## Simulation

Both `RUN SIMULATION` buttons run `--simulate`/`--backend simulated`
engine paths only. CALIBRATE offers four named scenarios
(`calibration_engine/simulation.py::NAMED_SCENARIOS`: HEALTHY, CLIPPED,
THERMAL_DRIFT, RFI_CONTAMINATED) - added to `almita_calibrate.py run
--scenario` as a small, real CLI feature so the web layer doesn't
reimplement scenario selection. Every simulated result is labeled
`SIMULATION` in the UI.

## Known gaps in this first iteration (Fase 73's "complete, not perfect")

- No `CANCEL` wiring (Fase 31) - simulated runs complete in seconds, so
  this was judged low-value for v1; a real long-running job would need
  it before going further.
- CALIBRATE's bandpass view draws the ADC histogram (real data, already
  returned by the run) rather than the full normalized-bandpass array,
  which `almita_calibrate.py run`'s own per-capture summary does not
  currently include - labeled honestly in the UI rather than fabricated.
- `RUN REAL` in CALIBRATE's web UI is not wired to hardware at all (see
  Safety above) - intentional scope boundary for this pass.

## Deployment

**NOT deployed to production `:8090` in this pass.** The `almita-observe-api.service`
unit was already engineered for safe restarts during an active campaign
(`KillMode=process` - its own docstring in the unit file documents this
was verified empirically: `capture.py`/`quicklook_live.py` children
survive a restart because they run `start_new_session=True` and are
reparented to init, outside the cgroup). That gives good reason to
believe a restart is safe - but the orchestrator's own in-memory
scheduling state was not independently re-verified end-to-end against a
live multi-day campaign in this pass, and the campaign that was RUNNING
throughout this work was never used as a test subject. Per explicit
instruction, no live restart was performed while in doubt.

To deploy, once the active campaign (`ALMITA-OBSERVE-20260917-20:10:26`
as of this writing) has ended or an operator explicitly accepts the
restart risk:

```bash
sudo systemctl restart almita-observe-api.service
sudo systemctl status almita-observe-api.service --no-pager
curl -s http://127.0.0.1:8090/api/align/status | head -c 200
```

All work was verified instead via a local dev server on an alternate
port (`python almita_orchestrator_server.py --host 127.0.0.1 --port 8091`),
never touching the live process or its port.
