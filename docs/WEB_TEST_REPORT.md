# ALMITA web hardening: test report

Branch `web-polish-v1` (from `864e59a`). No live hardware was used: INDI, MAIN/RFI SDR, the mount, `capture.py`, calibration and alignment are mocked or stubbed in every test and in every
screenshot. Chromium (already required by the console tests) runs the real pages headless with a stubbed `fetch`.

## Results

| suite | tests | result |
|---|---|---|
| `tests/test_web_hardening.py` (new: health layers, error contract, validation, path safety, conflicts, static/offline audit, shutdown) | 53 | pass |
| `tests/test_web_frontend.py` (new: real pages in headless Chromium, double submit, reload, dropped backend, XSS, LINK state, 30 min runs, 1024/768/480) | 22 | pass |
| `tests/test_almita_console.py` (existing Field Console: watcher + DOM snapshots) | 108 | pass, unchanged |
| `tests/test_almita_web_align_calibrate.py` | 22 | pass, unchanged |
| `tests/test_orchestrator_web.py` | 16 | 15 pass; **1 pre-existing time-of-day failure** (below); 2 tests updated for a stricter contract |
| `tests/test_almita_orchestrator_server.py`, `tests/test_dashboard.py` | 3 + 11 | pass |

**Pre-existing failure, not caused by this pass:** `test_plan_end_to_end_reflects_resolved_core_output` plans RA 6 h / Dec -30 deg and the planner refuses it when that sky is below the horizon
("predicted minimum altitude ... below planning floor"): it fails identically on the untouched `864e59a` tree (verified from a `git archive` copy) whenever it is run in the evening UTC. It needs a time-independent spec or a frozen clock.

**Contract changes covered by updated tests:** `POST /api/observe/start` with a `resolved_plan_path` that is not `data/mosaic/<campaign>/observation_resolved.json` is now **400** (was a 500 `FileNotFoundError` or, worse, an arbitrary file read).

## Test matrix (task section 88)

| scenario | where | result |
|---|---|---|
| all healthy | `test_all_healthy_is_ready_...` | READY, layers separate |
| INDI down | `test_indi_down_service_up_but_operational_not_ready` | service UP, operational NOT_READY |
| MAIN SDR busy / down / unknown | `test_main_sdr_busy_down_and_unknown` | BUSY / DOWN / UNKNOWN, NOT_READY |
| no observation / running / partial / failed | `test_no_observation_running_and_partial_observation_states`, observe page tests | states + `PARTIAL - 11 of 25 points captured (ABORTED)` |
| backend error | 400/404/405/409/500/503 tests | JSON body, request id, no traceback |
| empty data | console + calibrate tests | `No captures ...`, `No histogram data yet`, `N/A`, no `NaN`/`undefined` |
| reload during a running state | observe / align / calibrate reload tests | state rebuilt from the backend (poller resumed), not IDLE |
| two rapid clicks | `common.js` guard test; plan/start/stop/run in observe, align, calibrate; backend 409s | exactly one POST |
| API unavailable | observe / align / status tests | banner "backend not reachable ... you can retry", `LINK DISCONNECTED`, recovery when it returns |
| slow backend / long operations | `api()` timeout test; START/STOP kept at 130/150 s | `timeout` kind; a START timeout is *not* reported as "not started" |
| hostile strings (XSS) | observe, calibrate, console tests | shown as text, never executed |
| NaN / Infinity | `test_nan_and_infinity_never_reach_the_browser` | `null` in strict JSON |
| path traversal / NUL / bad plan path | asset, plan-path and session-id tests | 400/403/404, no file read |
| port already in use / SIGTERM / SIGINT | `test_port_conflict...`, `test_orchestrator_server_shuts_down_cleanly_on_signal` | exit 2 with a clear message; clean stop (exit 0) |
| systemd units (read-only audit) | `test_systemd_units_...` | ports, paths, `Restart`, `KillMode=process` consistent |

## Performance (mocked backends, this Pi)

* Static payloads: `common.js` 14 KB, `observe.js` 18 KB, `calibrate.js` 17 KB, `align.js` 11 KB, `status.js` 3 KB, `styles.css` 12 KB, largest page 6 KB; console `app.js` 28 KB.
* API latency (median of 20): `/healthz` 0.6 ms, `/api/system/health` 0.7 ms (cached 2 s), `/api/observe/status` 1.4 ms (15 KB body).
* Polling per open page: OBSERVE run panel 30/min + health strip 12/min; STATUS 12/min; Field Console 30/min; ALIGN status every 10 s; CALIBRATE status every **15 s** (was 5 s and it opened the rtl_tcp handshake each time).
  Hidden tabs do not poll (run panel: 15 s while hidden). One timer chain per poller: no overlapping requests, exponential backoff up to 30 s after failures.
* 30 virtual minutes with the page left open (OBSERVE running with the health strip, STATUS, and the Field Console): DOM node count stable (+/-5), live timers constant (<= 12 / <= 8), call rate constant, heap not linear.
* Log volume: successful polls are no longer logged (they used to print a line per request).

## Screenshots

`data/web_screenshots/` (untracked, not part of the commit): `observe|align|calibrate|status` at 1024x768, 768x1024 and 480x900, and `console8088` at 1024x768 and 768x1024,
generated from the real pages with mocked backends. Reviewed: desktop and tablet lay out without overflow; at 480 px forms stack to one column and the nav wraps.
Fixed after review: scenario/mode buttons all looked "active" (a more specific rule overrode `.active`), the `CENTER RA` row was always visible (`.obs-row{display:grid}` beat the `hidden` attribute), long tuner text overflowed its card.

## Known limitations

No authentication/TLS; the mount state is not observable read-only; no Firefox/Safari run (Chromium only); screenshots are not compared automatically; the WebGL 3D stack shows its error state in headless Chromium (no GPU).
