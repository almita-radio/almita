# ALMITA backlog

Operator requests not yet deployed. Each entry: request, status, where it lives, what is still pending.

## BL-001 — CALIBRATE Reference Wizard: Alt/Az preview of the planned points

**Requested 2026-10-02.** Show the points the wizard plans to use on an Alt/Az map, reusing ALIGN's sky view.
Requirements:
- azimuth, altitude and visiting order;
- reviewable before starting;
- refreshed when the plan changes;
- built from the same plan the wizard executes;
- reference instant shown, out-of-limit points flagged;
- never claims a zone is free of obstacles without local horizon data.

**Status:** integrated in `web-polish-v1` (2026-10-05) and extended: ALIGN's HI4PI layer + scale at the plan's own
instant (`hi4pi_map.sky_grid`), measured/active zones named, square centred canvases.
- `calibration_engine/hi_plan_preview.py` re-shapes the wizard's own saved `hi_plan`. It recomputes no positions.
- `almita_web_ops` adds `hi_plan_preview` to the wizard job facts.
- `console/calibrate.*` draws it with the shared `U.drawSkyView`, extended with optional order, warn and path; ALIGN's drawing is unchanged.

**Pending:** live check on the CALIBRATE page after the campaign. There is no local horizon model in the
project, so obstacles stay "NOT evaluated" until one exists.

## BL-002 — OBSERVE: selector for "Calibration profile path"

**Requested 2026-10-02.** A button that selects a calibration profile and fills the path. Requirements:
- manual entry kept;
- the path is validated: it exists, is a valid profile and is compatible with the observation config;
- clear errors;
- server disk distinguished from browser disk;
- browsing limited to the calibration directories, with nothing sensitive exposed;
- a browser-local file only by an explicit upload that returns a real server path.

**Status:** integrated in `web-polish-v1` (2026-10-05) and replaced by a server file explorer
(`/api/observe/calibration-profiles/browse`, BROWSE ALMITA SERVER…): folders, breadcrumbs, Hz/sps/dB and reason per file;
an unusable QUICKLOOK profile now BLOCKs the OBSERVE preflight. The text below describes the first version.
- `calibration_profile_catalog.py` lists and validates server profiles: only `<stem>.json` + `.npz` under
  `data/calibration`, no symlinks or `..` escaping it, and no other directories.
  - Validity is checked by `calibration_foundation.load_calibration_profile` (Quicklook's loader).
  - Compatibility is checked by `observation_preflight.planned_profile_compatibility`, the same decision the
    pre-RUN preflight now calls.
- `almita_orchestrator_server.py` adds two read-only endpoints:
  - `GET /api/observe/calibration-profiles` lists profiles;
  - `GET /api/observe/calibration-profiles/validate?path=` returns 400 with an operator message.
  Both accept the OBSERVE `center_frequency_hz` / `sample_rate` / `gain_db` as query parameters.
- OBSERVE page: a SELECT SERVER PROFILE… picker with compatibility per row and USE. Manual entry is kept and is
  validated on change, and again when frequency, rate or gain change. Paths from the browser's own disk
  (`C:\…`, `fakepath`, `file:`) get an explicit error.

**Not implemented (optional in the request):** uploading a profile from the browser's disk. It would need an
explicit upload endpoint that writes under `data/calibration/uploads/` and returns that server path. Today the
selector only accepts files already on the server.

**Pending:** live check on the OBSERVE page after the campaign (server restart needed for the new endpoints).

## Activating BL-001 / BL-002 (only after the running campaign has finished)

Nothing here was deployed. Merging alone changes the live UI, because `console/` is served from the source
tree on every request. Wizard and ops subprocesses also import the changed modules fresh. So merge only
when no observation, wizard or quicklook is running.

1. Check that nothing is active: `pgrep -af "capture[.]py|quicklook_live|calibrate_reference_wizard"` prints
   nothing, and the OBSERVE page shows COMPLETED or ABORTED.
2. On the deployment branch, from the main checkout:
   `git merge --no-ff backlog/wizard-preview-profile-picker`
3. Restart the web server so it loads `almita_orchestrator_server.py`, `almita_web_ops.py` and
   `observation_preflight.py`:
   `sudo systemctl restart almita-observe-api.service`.
   - The BL-002 endpoints and BL-001's `hi_plan_preview` facts need this restart.
   - The `console/` HTML/JS change on the next page load without it.
   - `rtl_tcp`, INDI and the console watcher do not need restarting.
4. Verify:
   - **OBSERVE:** SELECT SERVER PROFILE… lists `data/calibration/**/calibration_profile_v1.json` with
     COMPATIBLE/INCOMPATIBLE against the form values.
     - USE fills the path and the status line says "valid server profile · COMPATIBLE".
     - Typing `C:\fakepath\x.json` shows "NOT USABLE: … browser's computer".
     - `curl -u felipe 'http://localhost:8088/api/observe/calibration-profiles/validate?path=../observer_config.json'`
       returns 400.
   - **CALIBRATE wizard:** after PROPOSE HI ALTO / HI BAJO ZONES the sky preview shows
     "1 HI_ALTO" and "2 HI_BAJO" with a dashed path, the az/alt table, the reference instant and
     "OBSTACLES NOT EVALUATED".
     - RE-PROPOSE replaces it.
     - READY_HI_* shows the next zone highlighted.
     - ALIGN's sky view looks exactly as before.
5. Tests, which need no hardware:
   `.venv/bin/python -m pytest -q tests/test_hi_plan_preview.py tests/test_calibration_profile_catalog.py tests/test_web_frontend.py`

## BL-003 — status indicator centred on every page

**Requested and integrated 2026-10-05.** `.topbar > .hdr-status` (every page, and MONITOR's `<dl>`) is its own full-width,
centred row; checked at 1280 and 390 px before and after its content changes (tests/test_web_frontend.py).

## BL-004 — no operational use of the retired :8090

**Requested and integrated 2026-10-05.** Blackbox, forensics bench, comments and current docs name the unified :8088;
historical docs are marked as such; the console only uses relative URLs (tests/test_web_hardening.py).
