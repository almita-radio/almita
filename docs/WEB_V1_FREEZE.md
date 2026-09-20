# WEB V1: FREEZE

Status: **FROZEN / PASS.** This file closes the web hardening and polish pass. Detail lives in `WEB_ARCHITECTURE.md` (design and routes), `WEB_OPERATIONS.md`
(running it) and `WEB_TEST_REPORT.md` (evidence). After this freeze the web layer changes only for a demonstrated bug, a security finding, or an explicit new feature.

## Identity

| | |
|---|---|
| branch | `web-polish-v1` |
| commit before the pass | `864e59a` (SCIENCE forensics indoor bench) |
| final web commit | `23f9b5b` |
| files changed by the pass | 25 (backend glue, `console/`, three web docs, three test files, one docs index row); listing: `git diff --name-only 864e59a 23f9b5b` |
| local divergence from `origin/main` | at `23f9b5b`: **ahead 6 / behind 7**; with this freeze commit on top: **ahead 7 / behind 7** (both measured after `git fetch`). The 7 remote-only commits touch only `docs/site/index.html` and the Google verification file. **No rebase, merge or reset was done; publication/site reconciliation is a separate, later step.** |
| pushed | no |
| real hardware used (INDI, MAIN/RFI SDR, mount, `capture.py`, calibration, alignment) | **NO**: every test and screenshot uses mocks or stubs |

## Ports and entrypoints (unchanged)

| port | service | entrypoint |
|---|---|---|
| `8088` Field Console, read-only | `almita-console-web.service` (+ `almita-console-watcher.service`) | `almita_console_server.py` on `serve_dashboard.py` |
| `8090` Observe API + OBSERVE / ALIGN / CALIBRATE pages | `almita-observe-api.service` | `almita_orchestrator_server.py` (glue: `almita_web_align.py`, `almita_web_calibrate.py`, `almita_web_common.py`, new `almita_web_system.py`) |

Plain HTTP on the LAN, no authentication, no TLS (by design, documented; not changed in this pass).

## API acceptance

* Ports and every pre-existing route are preserved; no bookmark or page call was broken.
* **Additive routes:** `GET /healthz` (liveness only), `GET /api/system/health` (services, dependencies, workflows, operational readiness; cached 2 s), `GET /api/system/version`;
  pages `GET /status.html`, `/status.js`, `/common.js`; Field Console `GET /version.json`.
* **Observe routes** keep their raw-JSON shape. Errors add `ok`, `status`, `request_id`, `http_status` next to the old `error` key.
* **ALIGN / CALIBRATE keep the envelope: HTTP 200 with `blocked: true`** for policy and precondition outcomes.
* **HTTP codes:** 400 invalid input, 403 asset outside its root, 404 unknown, 405 method, 409 operational conflict, 500 unexpected (traceback only in the backend log, request id to the browser), 503 a dependency file/socket unavailable.
* **`POST /api/observe/start` is restricted** to `data/mosaic/<campaign>/observation_resolved.json` (anything else: 400). It still requires `confirm: true`; the backend flock/already-active check still answers 409 to a second start.
* JSON is strict: NaN/Infinity are serialised as `null`. HEAD is supported. Query strings do not break routing.

## Security acceptance

* Path traversal through START's plan path corrected (it was an arbitrary file read); NUL bytes and `..` are rejected (400/403/404); assets stay under `data/mosaic/` with three suffixes; session ids and replay/compare paths stay inside their roots.
* No `shell=True`, no `eval`/`exec`, no `os.system` in the web layer; the only subprocess calls use argv with a timeout. Enforced by a test.
* Dynamic data reaches the DOM through `textContent`; the read-only console's remaining `innerHTML` goes through an escaper on every dynamic value. Enforced by a test.
* No CORS headers (no wildcard). Every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: same-origin`; JSON is `Cache-Control: no-store`.
* Nothing from a request is interpolated into a command; no external URL is fetched (the only absolute URL in first-party code is the GitHub project link a user may click).

## Double submit, polling, reload

* **Double submit:** a guard ignores re-entry while a handler runs (PLAN, START, STOP, ALIGN/CALIBRATE runs, replay/compare); the backend is the authority: one plan at a time (409), one calibration at a time, no second ALIGN run on a session (`blocked`), a second START refused (409). STOP shows "requested", not "completed".
* **Polling:** one timer chain per poller (no overlap), exponential backoff to 30 s, no polling while the tab is hidden; CALIBRATE status 15 s and no rtl_tcp handshake while an observation or quicklook owns MAIN; successful polls are not logged.
* **Reload recovery:** OBSERVE rebuilds an active or terminal run from the backend; ALIGN and CALIBRATE adopt a still-running simulation job. No page assumes IDLE after a reload.

## Health / readiness

`/api/system/health` keeps three layers apart: **services** (observe API, field console, console watcher: UP/STALE/DOWN), **dependencies** (INDI listening; MAIN SDR AVAILABLE/BUSY/DOWN/UNKNOWN; RFI SDR optional;
**mount `NOT_EXPOSED`**, never invented) and **operational** (READY / DEGRADED / NOT_READY with reasons). A running API with INDI down is "service UP, operational NOT_READY". All reads are passive (`/proc`, sysfs, runtime JSON, the job table); the view never connects to INDI or rtl_tcp.

## UX acceptance

Active page marked (`aria-current`); LIVE / STALE / DISCONNECTED link state in every header; the reason a button is disabled is visible; every time is labelled UTC; frequencies in MHz (RA in hours, Dec in degrees, sizes in GiB/MiB); a stopped or failed campaign
reads `PARTIAL - N of M points`; **no invented ETA** (a labelled estimate appears only after 5 points); empty, NaN and non-finite values show explicit empty states, never `NaN`/`undefined`; layout checked at 1024, 768 and 480 px without horizontal overflow; fully offline (no CDN, no fonts, no external script).

## Systemd audit (read-only)

`almita-console-web.service` and `almita-observe-api.service`: `ExecStart` = `/home/stellarmate/almita/.venv/bin/python <script>` with the documented ports, `WorkingDirectory=/home/stellarmate/almita`, `Restart=on-failure` (5 s); the observe unit keeps `KillMode=process`
so an observation survives an API restart. **No unit file was changed and no service was restarted.**

## Production note

* **8090 serves `console/` dynamically** (files are read on every request): a page or script change is live without a restart. Python changes on 8090 (backend, routes) need a restart of `almita-observe-api.service`.
* **8088 serves a prepared copy** (`data/console_web`, rebuilt when the unit starts) and the Python server code: `almita-console-web.service` must be restarted to load the new console, `common.js` and `version.json`.
* **The running services still execute the code that was loaded before this pass.** Restarting them is the operator's action and was NOT done. (Restarting `almita-observe-api.service` does not stop a running observation, but it clears the in-memory ALIGN/CALIBRATE simulation job table.)

## Tests (exact)

| suite | result |
|---|---|
| `tests/test_web_hardening.py` | 53 PASS |
| `tests/test_web_frontend.py` | 22 PASS |
| `tests/test_almita_console.py` (existing Field Console) | 108 PASS |
| `tests/test_almita_web_align_calibrate.py` (ALIGN/CALIBRATE) | 22 PASS |
| `tests/test_orchestrator_web.py` | 15 PASS, **1 FAIL (pre-existing, below)** |
| `tests/test_almita_orchestrator_server.py` | 3 PASS |
| `tests/test_dashboard.py` | 11 PASS |

Total: 234 PASS, 1 FAIL (per-file runs, last verified at this freeze).

### The one failure is pre-existing

`tests/test_orchestrator_web.py::test_plan_end_to_end_reflects_resolved_core_output` asks the planner for RA 6 h / Dec -30 deg and expects a `PASS` visibility. The planner (core, not the web layer) answers
"predicted minimum altitude ... falls below planning floor" whenever that part of the sky is below the horizon, so the test depends on the time of day.

* It **reproduces on a clean checkout of `864e59a`** (before this pass), verified from a `git archive` copy of that commit.
* It is **not a regression introduced by WEB POLISH V1**, and it was deliberately **not modified** here (fixing it needs a time-independent spec or a frozen clock: a separate change).

## Frozen core (verified unchanged)

No file changed between `864e59a` and `23f9b5b`, and the working tree is clean, for: `reduce_engine`, `science_engine`, `science_forensics`, `capture.py`, `observation_orchestrator.py`, `sdr_capture.py`, `indi_telescope_control.py`, `systemd/`, `examples/science_forensics_experiment.yaml`.

## Performance (recorded, not re-optimised)

First-party JS 3-28 KB (`status.js` 3 KB ... console `app.js` 28 KB), CSS 12 KB, largest page 6 KB; `/healthz` about 0.6 ms, `/api/system/health` about 0.7 ms (cached), `/api/observe/status` 1.4 ms (15 KB).
Polling: OBSERVE run panel 30/min + health strip 12/min, STATUS 12/min, Field Console 30/min, ALIGN status every 10 s, CALIBRATE status every 15 s. In a 30-minute virtual-time test (OBSERVE running with the strip, STATUS, and the Field Console) DOM node count, live timers, call rate and heap show no linear growth.

## Screenshots

14 local screenshots exist in `data/web_screenshots/` (4 pages x 1024/768/480 px, plus the console at 1024 and 768). They are **untracked and not part of any commit**.

## Known limitations

No authentication or TLS; the mount state is not observable read-only; ALIGN/CALIBRATE web runs are simulations; Chromium only; the 3D WebGL panel shows its error state in headless (no GPU); the pre-existing time-of-day test failure above; production services keep running the old code until restarted.

## Freeze rule

After this commit the web layer changes only for: a demonstrated bug, a security finding, a change in the backend contracts it presents, or an explicit new feature. No cosmetic churn: every change needs a test and a line in `WEB_TEST_REPORT.md`.
