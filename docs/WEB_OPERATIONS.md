# ALMITA web: operations

ONE web server, ONE port: `almita-observe-api.service` runs `almita_orchestrator_server.py --port 8088` and serves the Field
Console (MONITOR), every operating page, the API, `runtime/` files, result files and the mount-camera MJPEG stream. The former
`almita-console-web.service` (unauthenticated `:8088`) and the `:8090` port are retired. Other units unchanged:
`almita-console-watcher.service`, `almita-system-blackbox.service`. Captures, quicklook and REDUCE/SCIENCE jobs stay separate processes.

## Authentication (single user, HTTP Basic)

* Every request (pages, API, files, stream, STOP/START actions) needs the user + password; the browser asks once per session.
* Credentials: `~/.config/almita/web_auth.json` (outside the repo, mode 0600, PBKDF2-SHA256 hash only; override with
  `ALMITA_WEB_AUTH_FILE`). Missing, malformed or group/other-readable file -> every request gets 503: access stays closed.
* Set / change (applies without restart): `./.venv/bin/python almita_web_auth.py set-password --user <name>`; check: `... check`.
* State-changing requests from another origin are refused (403, CSRF guard on `Origin` / `Sec-Fetch-Site`).
* No logout with Basic: close the browser. **Plain HTTP: the password travels readable.** Fine on the field LAN; before any
  Internet exposure put TLS in front (no certificate exists on the Pi today) or reach it only through a VPN/SSH tunnel.
* curl: `curl -u <user> http://<host>:8088/api/system/health` (prompts for the password; do not put it on the command line).

## Activation (once, when no capture/ALIGN/CALIBRATE job is in progress)

```
./.venv/bin/python almita_web_auth.py set-password --user <name>
sudo cp systemd/almita-observe-api.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl disable --now almita-console-web.service      # frees :8088 (static monitor only; owns no capture)
sudo systemctl restart almita-observe-api.service            # KillMode=process: running capture/quicklook children survive
```
Revert: `git revert <commit>`, copy back both unit files from the previous commit, `daemon-reload`,
`enable --now almita-console-web.service`, `restart almita-observe-api.service`.

## URLs

| what | URL |
|---|---|
| Field Console (MONITOR) | `http://<host>:8088/` |
| OBSERVE / ALIGN / CALIBRATE / PIPELINE / REDUCE / SCIENCE | `http://<host>:8088/observe.html`, `/align.html`, ... |
| System status (services, dependencies, operational readiness) | `http://<host>:8088/status.html` |
| liveness / health / build | `/healthz`, `/api/system/health`, `/api/system/version`, `/version.json` (all on :8088, all authenticated) |

## Reading the status

* Header chips on every operating page: `OBSERVATION`, `MAIN SDR`, `INDI`, `LINK`. **LINK** is about the page's own connection: `LIVE` (last poll worked, data fresh), `STALE` (no success for a
  while), `DISCONNECTED` (repeated failures: the API is down or the network is lost; the page retries with backoff and recovers alone). The Field Console header has the same LINK plus `UPDATED (UTC)`.
* **A running API is not a ready instrument.** `status.html` shows three layers: services, dependencies, operational. `NOT_READY` lists the reasons (INDI not listening, MAIN SDR busy/down, an observation active).
  The mount is `NOT_EXPOSED` on purpose: nothing read-only can tell.
* OBSERVE run panel: `RUNNING / STOPPING / COMPLETED / ABORTED / FAILED / DEGRADED` plus a plain reading (`PARTIAL - 11 of 25 points captured (ABORTED)`). No ETA is invented: a remaining-time estimate appears only after 5 points,
  labelled as an estimate from the average pace. **STOP REQUESTED is not completion**: the note stays until the orchestrator reports the final state (up to two minutes).
* Buttons that are disabled say why (tooltip and text): `plan first`, `an observation is already RUNNING`, `the plan is stale: re-plan`, `blocked by policy ...`.

## Start / stop / restart (existing commands)

```
sudo systemctl status  almita-observe-api.service almita-console-watcher.service
sudo systemctl restart almita-observe-api.service          # Python changes; also re-copies console/ into data/console_web + version.json
sudo journalctl -u almita-observe-api.service -n 100 --no-pager
```
* Changing files under `console/` needs **no restart for the operating pages** (read per request) but **does for MONITOR** (`index.html`/`app.js`: its `data/console_web` copy is rebuilt at start).
* Restarting `almita-observe-api.service` **does not stop a running observation** (`KillMode=process`: capture.py and quicklook are detached children). It does lose the API's in-memory ALIGN/CALIBRATE simulation job table.
* Stop an observation from the page (**STOP OBSERVATION**, confirmed) or from a shell with SIGINT to the capture PID. Never SIGTERM/SIGKILL.
* The units run `ExecStart=/home/stellarmate/almita/.venv/bin/python ...` with `WorkingDirectory=/home/stellarmate/almita` and `Restart=on-failure` (5 s).

## Manual runs (development, no systemd)

```
./.venv/bin/python almita_orchestrator_server.py --host 127.0.0.1 --port 8099 --public-root /tmp/almita_pub   # dev: keep the live data/console_web untouched
```
If the port is taken the process exits with `port already in use` (exit code 2) and does not touch the other process. Ctrl-C and SIGTERM stop it cleanly.

## Troubleshooting

| symptom | meaning / action |
|---|---|
| header `LINK DISCONNECTED`, banner "backend not reachable" | the web server is down or unreachable: `systemctl status almita-observe-api.service`, `curl -u <user> http://localhost:8088/healthz` |
| banner "no response within N s (backend slow or busy...)" | the request was cut by its timeout; START/STOP may still have taken effect: look at the run panel / `status.html` |
| `LINK STALE` on the console | the watcher stopped updating `data/runtime/almita_status.json`: `systemctl status almita-console-watcher.service` |
| START refused with 409 | an observation is already active (the backend decides; the page also disables START) |
| PLAN refused with 409 "planning already in progress" | one plan at a time (matplotlib on the Pi); wait for the first |
| a red error banner with `[request abc12345]` | search the journal for `req=abc12345`: the traceback is there, not on screen |
| `503 dependency unavailable` | a file the route needs (runtime JSON, config) is missing or unreadable |
| `MAIN SDR BUSY` | an observation, quicklook or calibration owns rtl_tcp; the status route does not probe it meanwhile |
| `RFI SDR DOWN` | optional: the second dongle (serial 00000002) is not attached or RFI_REF failed |

## Indoors / offline

Everything is served locally; no page loads anything from the Internet. Indoors, the antenna and mount are not deployed: use `scripts/science_forensics_bench.py`
(and `INDOOR_MODE=1` for the field wrapper); START on the OBSERVE page still moves the mount and must not be used indoors.

## Limitations

HTTP LAN without authentication; no TLS in this pass; the mount state cannot be read by the read-only stack; ALIGN/CALIBRATE web runs are simulations.
