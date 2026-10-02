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

**Status:** implemented on branch `backlog/wizard-preview-profile-picker`. Not deployed.
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

**Status:** implemented on branch `backlog/wizard-preview-profile-picker`. Not deployed.
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
