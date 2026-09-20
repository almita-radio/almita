# ALMITA web: architecture

Two resident HTTP processes, both **plain HTTP on the LAN with no authentication and no TLS** (documented, unchanged): the web is an
interface to the instrument, never the owner of its logic. Every number and every permission comes from the backend; the browser only makes the
real state visible and makes it hard to operate the instrument wrongly.

| | Field Console | Observe API + pages |
|---|---|---|
| port | `8088` | `8090` |
| unit | `almita-console-web.service` (+ `almita-console-watcher.service`) | `almita-observe-api.service` |
| entrypoint | `almita_console_server.py` (read-only static server: `serve_dashboard.py`) | `almita_orchestrator_server.py` (`ThreadingHTTPServer`) |
| writes | none (GET/HEAD only; POST/PUT/DELETE/PATCH -> 405) | JSON POSTs that call the existing OBSERVE / ALIGN / CALIBRATE functions |
| content | `/` monitor (`console/index.html`, `app.js`, `spectral_stack_3d.js`, vendored three.js), `/runtime/*.json` (symlink to `data/runtime/`), `/version.json` | `/observe.html`, `/align.html`, `/calibrate.html`, `/status.html`, `/common.js`, `/styles.css`, JSON API |

Other pieces: `almita_console_watcher.py` (read-only status writer, every 2 s, produces `data/runtime/almita_status.json`),
`almita_web_align.py` / `almita_web_calibrate.py` (route glue to `almita_align.py` / `almita_calibrate.py`, simulation only),
`almita_web_common.py` (envelope, MAIN-SDR ownership check, job registry), `almita_web_system.py` (health + version, new), and the legacy static
`dashboard/` (built by `dashboard/build_dashboard.py`, served by `serve_dashboard.py`; not a running service).

## Routes (8090)

| route | method | what | contract |
|---|---|---|---|
| `/`, `/observe.html`, `/align.html`, `/calibrate.html`, `/status.html` | GET/HEAD | pages (`/` redirects to OBSERVE) | HTML, `no-store` |
| `/common.js`, `/observe.js`, `/align.js`, `/calibrate.js`, `/status.js`, `/styles.css` | GET/HEAD | static, from `console/` | `no-store` |
| `/healthz` | GET/HEAD | **liveness only**: the process answers | `{ok,status:"ALIVE",data}` |
| `/api/system/health` | GET | services, dependencies, workflows, operational readiness, version (cached 2 s) | `{ok,status,message,data}` |
| `/api/system/version` | GET | git short SHA, start time, hostname, transport | `{ok,status,data}` |
| `/api/observe/defaults`, `/api/observe/status` | GET | defaults / orchestrator + acquisition state | raw JSON (unchanged) |
| `/api/observe/plan` | POST | validate spec + plan (heavy: one at a time) | raw JSON |
| `/api/observe/start` | POST | needs `confirm:true` and a plan under `data/mosaic/<campaign>/observation_resolved.json` | raw JSON |
| `/api/observe/stop` | POST | needs `confirm:true`; SIGINT to the tracked capture; blocks until it exits (up to 120 s) | raw JSON |
| `/api/observe/plan-assets/<dir>/<file>` | GET | `.png/.json/.csv` under `data/mosaic/` only | file |
| `/api/align/*`, `/api/calibrate/*` | GET/POST | status, plan, preflight, run (simulation), replay, compare, sessions, profile | envelope `{ok, blocked, reason, data}` |

## Contracts

* **Observe routes** keep their established raw-JSON shape; errors are `{"error": ..., "ok": false, "status": "ERROR", "request_id": ...}`
  with the HTTP codes below. `error` is the key the pages have always read.
* **ALIGN / CALIBRATE** keep the envelope with **HTTP 200 and `blocked:true`** for policy/precondition outcomes (a blocked preflight is a result, not a
  transport failure; the existing tests and pages depend on it). Malformed input, unknown routes and server faults use the error codes below.
* **System routes** use `{ok, status, message?, data}`.
* **HTTP codes:** 200 ok; 400 invalid input (bad JSON, bad `Content-Length`, missing `confirm`, invalid plan path, invalid spec); 403 asset path outside its
  root; 404 unknown route/asset/plan; 405 method; 409 operational conflict (an observation is already active, a plan is already being planned, blocked
  by the planner); 500 unexpected (traceback in the backend log only; the browser gets a short message and the request id); 503 a file/socket the route needs is unavailable.
* **Every response** carries `X-Request-Id`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: same-origin`; JSON is
  `Cache-Control: no-store`. **NaN / Infinity are serialised as `null`** (strict JSON). **No CORS headers** (pages and API share an origin).
* **Logging** (stdout -> journal): `<UTC ISO> <LEVEL> almita_orchestrator_server req=<id> ...`; successful polls are not logged, failures and actions are.

## Layers of state (never one green "OK")

`/api/system/health` keeps three layers apart:

1. **services**: `observe_api`, `field_console` (port 8088 listening), `console_watcher` (`almita_status.json` fresher than 10 s) -> `UP / STALE / DOWN`.
2. **dependencies**: `indi` (server port 7624 listening), `mount` (**`NOT_EXPOSED`**: the read-only stack does not connect to it and does not pretend),
   `main_sdr` (`AVAILABLE / BUSY / DOWN / UNKNOWN`: rtl_tcp listening + the existing ownership check `get_sdr_resource_status`), `rfi_sdr` (optional; USB serial 00000002 / RFI_REF status).
3. **operational**: `READY / DEGRADED / NOT_READY` derived only from the two layers above, with the reasons. *Service UP is not operational READY.*
   Workflows (`observation`, `calibration`, `alignment`) are reported next to it. Everything is read passively (`/proc/net/tcp`, sysfs USB, runtime JSON, the job table): the health
   view never connects to INDI or rtl_tcp.

## Front end

No framework, no external request, no CDN, no font service. `console/common.js` (`window.AlmitaUI`) is shared by the 8090 pages (and loaded, optionally, by the console):

* `api()` never throws: explicit `timeout / network / http / parse` errors with endpoint, message, retry hint and request id; per-call timeouts (default 15 s;
  START 130 s, STOP 150 s, PLAN 120 s: real long operations are not cut short).
* `guard()` ignores re-entry while a handler runs (double click / Enter / two tabs' fast clicks never become two POSTs); `setEnabled()` always shows **why** a button is disabled.
* `poller()` is one timer *chain* (no overlap, exponential backoff after failures, stops on demand, pauses while the tab is hidden) with the states
  `CONNECTING / LIVE / STALE / DISCONNECTED`; every page header shows the LINK state and the compact health strip (OBSERVATION, MAIN SDR, INDI).
* Backend text is inserted with `textContent` (or escaped before the few remaining `innerHTML` in the read-only console). Times are always labelled UTC;
  frequencies in MHz, RA in hours, Dec in degrees, sizes in GiB/MiB.
* Page reload during a run rebuilds the state from the backend (OBSERVE adopts an active/terminal run; ALIGN and CALIBRATE adopt a running simulation job).

## Safety model (unchanged authority, more explicit)

* START and STOP require `confirm:true`; the browser's confirmation dialog lists campaign, points, MAIN centre/rate/gain, duration and the mount requirements. The backend refuses a second
  start (flock + already-active check -> 409) whatever the button says; STOP signals the exact tracked PID with SIGINT only.
* `resolved_plan_path` must be `data/mosaic/<campaign>/observation_resolved.json`; asset serving is limited to `data/mosaic/` and three suffixes; session ids and replay/compare paths
  must resolve inside `data/alignment` / `data/calibration`; nothing from a request reaches a shell (no `shell=True`, no `eval`).
* `/api/calibrate/status` no longer opens the MAIN rtl_tcp handshake while an observation or quicklook owns it.
* ALIGN and CALIBRATE from the web are **simulation only**: real hardware runs are not reachable from these endpoints.

## Dependencies and failure modes

| if this is down | the web does |
|---|---|
| INDI | starts and serves; `indi DOWN`, operational `NOT_READY` (health strip shows it) |
| rtl_tcp / MAIN SDR | starts; `main_sdr DOWN` (or `BUSY` during an observation) |
| console watcher | Field Console shows `STALE`/`DISCONNECTED`; health `DEGRADED` |
| the API (8090) | pages show `LINK DISCONNECTED` and a "backend not reachable" banner; they recover on their own when it returns; the running observation is independent of the API (KillMode=process) |
| a runtime file is unreadable | that row is `UNKNOWN`, the rest of the view survives; API routes answer 503 with a request id |
| port 8088 / 8090 already taken | the server exits with a clear message (`port already in use`); nothing is killed; systemd retries every 5 s |

## Known limitations

HTTP LAN without authentication or TLS (by design; put it behind the LAN boundary); the mount state is not exposed by any read-only source; the
RFI SDR row reflects USB presence and the watcher's RFI_REF status, not a live measurement; ALIGN/CALIBRATE web runs are simulations; `almita_web_calibrate.run_simulation` still patches
`CalibrationSession.__init__` to learn the session id (pre-existing, serialised by a lock).
