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

**Status:** see the BL-002 commit on the same branch.
