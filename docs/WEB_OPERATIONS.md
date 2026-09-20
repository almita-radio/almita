# ALMITA web: operations

Existing services (unchanged): `almita-console-web.service` (`:8088`), `almita-observe-api.service` (`:8090`), `almita-console-watcher.service`, `almita-system-blackbox.service`.
Pages and API are plain HTTP on the LAN (no TLS, no login).

## URLs

| what | URL |
|---|---|
| Field Console (monitor, read-only) | `http://<host>:8088/` |
| OBSERVE / ALIGN / CALIBRATE | `http://<host>:8090/observe.html`, `/align.html`, `/calibrate.html` |
| System status (services, dependencies, operational readiness) | `http://<host>:8090/status.html` |
| liveness / health / build | `http://<host>:8090/healthz`, `/api/system/health`, `/api/system/version` (console: `:8088/version.json`) |

## Reading the status

* Header chips on every 8090 page: `OBSERVATION`, `MAIN SDR`, `INDI`, `LINK`. **LINK** is about the page's own connection: `LIVE` (last poll worked, data fresh), `STALE` (no success for a
  while), `DISCONNECTED` (repeated failures: the API is down or the network is lost; the page retries with backoff and recovers alone). The Field Console header has the same LINK plus `UPDATED (UTC)`.
* **A running API is not a ready instrument.** `status.html` shows three layers: services, dependencies, operational. `NOT_READY` lists the reasons (INDI not listening, MAIN SDR busy/down, an observation active).
  The mount is `NOT_EXPOSED` on purpose: nothing read-only can tell.
* OBSERVE run panel: `RUNNING / STOPPING / COMPLETED / ABORTED / FAILED / DEGRADED` plus a plain reading (`PARTIAL - 11 of 25 points captured (ABORTED)`). No ETA is invented: a remaining-time estimate appears only after 5 points,
  labelled as an estimate from the average pace. **STOP REQUESTED is not completion**: the note stays until the orchestrator reports the final state (up to two minutes).
* Buttons that are disabled say why (tooltip and text): `plan first`, `an observation is already RUNNING`, `the plan is stale: re-plan`, `blocked by policy ...`.

## Start / stop / restart (existing commands)

```
sudo systemctl status  almita-observe-api.service almita-console-web.service almita-console-watcher.service
sudo systemctl restart almita-console-web.service          # re-copies console/ into data/console_web and re-writes version.json
sudo systemctl restart almita-observe-api.service          # picks up Python and page changes (pages are read from console/ on every request)
sudo journalctl -u almita-observe-api.service -n 100 --no-pager
sudo journalctl -u almita-console-web.service -n 100 --no-pager
```
* Changing files under `console/` needs **no restart for the 8090 pages** but **does for the 8088 console** (its `data/console_web` copy is rebuilt at start). Python changes need a restart of the unit.
* Restarting `almita-observe-api.service` **does not stop a running observation** (`KillMode=process`: capture.py and quicklook are detached children). It does lose the API's in-memory ALIGN/CALIBRATE simulation job table.
* Stop an observation from the page (**STOP OBSERVATION**, confirmed) or from a shell with SIGINT to the capture PID. Never SIGTERM/SIGKILL.
* Nothing in this pass changes a unit file. The units run `ExecStart=/home/stellarmate/almita/.venv/bin/python ...` with `WorkingDirectory=/home/stellarmate/almita` and `Restart=on-failure` (5 s).

## Manual runs (development, no systemd)

```
./.venv/bin/python almita_orchestrator_server.py --host 127.0.0.1 --port 8090
./.venv/bin/python almita_console_server.py --bind 127.0.0.1 --port 8088
```
If the port is taken the process exits with `port already in use` (exit code 2) and does not touch the other process. Ctrl-C and SIGTERM stop both cleanly.

## Troubleshooting

| symptom | meaning / action |
|---|---|
| header `LINK DISCONNECTED`, banner "backend not reachable" | 8090 is down or unreachable: `systemctl status almita-observe-api.service`, `curl -s http://localhost:8090/healthz` |
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
