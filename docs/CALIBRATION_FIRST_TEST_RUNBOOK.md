# Calibration — First Real Test Runbook (DESIGNED, NOT EXECUTED)

This document designs the smallest useful real calibration test (Fase 51).
**No command in this document has been run against real hardware in this
pass.** ALMITA's deployment state is not confirmed FIELD as of this
writing, and this pass's own hardware policy (Fase 50) is offline-only:
simulation, replay, and code audit. This runbook exists so that when a
human operator separately authorizes it, the steps are already thought
through instead of improvised at the keyboard.

## Why this shape, specifically

- **No mount movement.** Calibration V1 does not need a raster or a
  target — a fixed input (whatever is currently connected: antenna, or a
  50-ohm load if one is deliberately connected) at the current receiver
  configuration is enough for every measurable quantity in
  `docs/CALIBRATION_SCOPE.md`'s "we CAN measure now" table.
- **No gain change.** The current operational gain (40.2 dB) is used
  as-is. A gain sweep is a SEPARATE, later, separately-authorized step
  (Fase 9's own instruction) — this first test is deliberately narrower.
- **Repeat, don't vary.** Several short captures back-to-back tests
  repeatability (Fase 15) before anything else is asked of the instrument.

## Step 1 — Confirm deployment context (always safe, read-only)

```bash
python almita_calibrate.py audit
```

Records (does not require) the current `DeploymentState`. Calibration V1
does not require FIELD (Fase 5) — BENCH/INDOOR results are valid for
ADC/clipping/frequency-axis conclusions, just not for sky-bandpass/RFI-
environment/antenna-response ones (Fase 68). Whatever the state is, it
gets written into the session automatically.

## Step 2 — Preflight

```bash
python almita_calibrate.py preflight --receiver MAIN --session-root data/calibration
```

Checks: receiver identity recognized, disk space, output directory
writable. Deployment state is reported but never blocks (see Fase 5 in
the audit above). `PREFLIGHT: READY` or `BLOCKED` with a named reason.

## Step 3 — The test itself (once authorized)

```bash
python almita_calibrate.py run --backend real --receiver MAIN \
    --session-root data/calibration \
    --center-frequency-hz 1420405752 --sample-rate-hz 2400000 \
    --gain-db 40.2 --bias-t-state ON \
    --n-captures 5 --capture-seconds 2.0
```

**This exact command is REFUSED by this pass's own code** — `--backend
real` always returns `REFUSED` with no session written (see
`almita_calibrate.py cmd_run`). Removing that refusal is the explicit,
separate authorization this document is designed for, not something this
pass grants itself.

What it would do once authorized: 5 back-to-back 2-second captures at the
current operational configuration, no gain change, no mount movement,
each analyzed for raw sample statistics, clipping/headroom, and relative
bandpass shape; stability across the 5 captures computed from the same
run. Evidence written under `data/calibration/CAL-<UTC timestamp>/`
exactly as `almita_calibrate.py run --backend simulated` already produces
today (verified in this pass via `test_almita_calibrate_cli.py`) — the
real and simulated paths share every analysis function, only the
acquisition backend differs.

## Step 4 — Read the result

```bash
python almita_calibrate.py result data/calibration/CAL-<timestamp>
```

Expect `calibration_level: OPERATIONAL_RELATIVE`,
`absolute_calibration: false`, a `quality.verdict` of GOOD/MARGINAL/BAD/
INCONCLUSIVE with explicit `reasons`, and clipping/stability/bandpass
numbers — no Kelvin, no dBm, no noise figure anywhere in the output.

## Step 5 — Replay (repeatable, offline, any time after)

```bash
python almita_calibrate.py replay data/calibration/CAL-<timestamp>
```

Re-runs the same analysis on the same raw captures — useful once
thresholds or the bandpass method evolve, without re-observing.

## Only after this — a separate authorization for a gain sweep

```bash
python almita_calibrate.py gain-sweep --around-gain 40.2 --n-steps 6 --dry-run
```

is safe to run any time (it never touches hardware — it only prints the
candidate plan). Actually stepping rtl_tcp's gain across that plan against
real hardware is Fase 9's real test, and is explicitly a LATER,
SEPARATELY authorized step, not bundled into this first test.

## What this runbook deliberately does NOT include

- SYNC — not applicable to calibration at all in this codebase.
- A gain change of any kind.
- A PPM/frequency correction of any kind.
- Any mask application to science.
- Any profile activation (a profile built afterward via
  `almita_calibrate.py profile build` is `DRAFT` and stays `DRAFT`).
