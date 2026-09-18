# HI First-Light Runbook

Operational procedure for the first real HI night scan(s) with ALMITA.
Every command below is a REAL, currently-working command in this
repository (verified against `almita_align.py` and `hw_hi_night_scan.py`
as of this pass) - none of it is aspirational.

**SYNC is NOT part of this procedure.** This runbook never syncs the
mount. `alignment_phase=FIRST_LIGHT_HI` makes SYNC unconditionally
unavailable in software (`alignment_engine/hi/sync_policy.py`) - there is
no flag or flow in this document that reaches it.

**No auto-start.** There is no scheduler in this codebase that watches
target visibility and starts a scan on its own. `hi plan`/preflight can
tell you *when* a target is visible, but a human runs the actual scan
command, at the keyboard, when they choose to. This is deliberate (an
unattended first real-hardware campaign is not something this project
does) - see FIRST-LIGHT CODE FREEZE below.

**Code freeze.** As of this pass, the first-light HI path (fitter, target
selector, bootstrap, quality thresholds, raster geometry) is FEATURE
FROZEN UNTIL FIELD TEST: no further tuning "for sport," no threshold
changes, no raster changes, until real field data comes back and shows an
actual problem. Only clear bug fixes are in scope until then.

---

## 0. Before you touch anything: confirm deployment state

Physical deployment is never inferred (not from the clock, not from GPS,
not from "the mount is reachable over the network") - it is an explicit,
persisted operator statement, and it defaults to blocking real hardware
movement until you set it.

```bash
# Check current state (safe, read-only, always OK to run)
python almita_align.py deployment show

# Only when ALMITA is ACTUALLY set up outside, pointed at open sky:
python almita_align.py deployment set-field --confirm --reason "roof deployment 2026-09-18"

# When you pack up / bring it back inside - do this BEFORE moving it:
python almita_align.py deployment set-indoor --confirm --reason "packed up for the night"
```

If you skip this, or the state is UNKNOWN/BENCH/INDOOR/missing/corrupt,
`hw_hi_night_scan.py --backend real` will refuse at its very first
preflight gate (`deployment_state_is_field`) - this is fail-closed by
construction, there is no override flag.

## 1. Confirm the reference is intact

```bash
python almita_align.py hi reference-inspect data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.fits

python almita_align.py hi reference-validate \
    data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.fits \
    data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.manifest.json
```

`reference-validate` re-checks the checksum, structure, and physical
plausibility of the FITS file against its manifest - run it if you have
any doubt the file on disk hasn't changed since it was validated (a full
disk, a bad copy, anything unusual).

## 2. Dry rehearsal (simulated backend - no hardware at all)

Always do this BEFORE a real run, and any time you've changed anything
upstream (config, raster, integration time):

```bash
python hw_hi_night_scan.py --backend simulated \
    --session-root data/alignment \
    --fits-path data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.fits \
    --manifest-path data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.manifest.json
```

This exercises the full pipeline (target selection, raster, acquisition,
fit, Quality V2, bootstrap, evidence writing) with a simulated mount and a
simulated SDR - nothing physical moves. Confirm it reaches `PASS` (exit
code 0) with a real-looking `[TARGET]` line and a sane `estimated
duration` before proceeding.

## 3. The real scan

```bash
python hw_hi_night_scan.py --backend real --yes \
    --session-root data/alignment \
    --host localhost --port 7624 \
    --sdr-host localhost --sdr-port 1234 \
    --fits-path data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.fits \
    --manifest-path data/reference/hi/hi4pi/derived/CAR_E01_moment_v100kms.manifest.json
```

What happens, in order:

1. **System preflight** - deployment state, no conflicting capture, no
   other active session, reference exists/parses/validates, disk space,
   output writable, mount reachable, observer location valid, mount not
   parked, SDR reachable, original tracking mode known. Any failure here
   prints `FIRST REAL HI NIGHT SCAN: BLOCKED` with the exact reason and
   exits 2 - nothing is touched.
2. **Target selection + astronomical preflight** - picks a real target
   from the FITS reference for right now, checks every raster point stays
   within your altitude limits for the whole estimated duration.
3. **Human preflight summary** - printed to the terminal: DEPLOYMENT,
   MODE, REFERENCE, TARGET, GRID, ESTIMATED DURATION, TRACKING, MOUNT
   WRITES, SYNC, RAW DATA, FREE DISK, then `Proceed? [y/N]:`. Type `y` to
   continue. This is in addition to `--yes` - `--yes` says "this script is
   allowed to touch real hardware at all," this prompt says "given
   exactly this target/grid/duration, go." Anything other than `y`/`yes`
   aborts cleanly (exit 6), no mount movement.
4. The actual raster: GOTO each point, settle, acquire, reduce. Ctrl-C at
   any point cancels cleanly - see ABORT/RECOVERY below.
5. Fit + Quality V2 (with bootstrap) if coverage is sufficient.
6. SYNC eligibility is printed and is always `BLOCKED BY FIRST_LIGHT_HI
   POLICY` - this is expected, not an error.
7. Final verdict (`PASS`/`PARTIAL`/`FAIL`/`CANCELLED`/`BLOCKED`) and exit
   code, plus the evidence directory path.

## 4. Status / result of a session afterward

```bash
cat <session_dir>/hardware_test_result.json | python -m json.tool
cat <session_dir>/logs/session.log
```

Every session directory (`data/alignment/HI-<UTC timestamp>/`) contains:
`alignment_config.json` (the exact args used), `target.json`,
`raw_grid.json` (every point's raw validity + metric), `fit_result.json` +
`quality.json` + `bootstrap.json` (if coverage was sufficient),
`write_audit.json` (every mount write actually issued - only for
`--backend real`), `deployment_state.json` (only for `--backend real`),
`logs/session.log` (a line per state transition/gate/point), and
`points/point_NNNN.h5` (the raw IQ per point, `--backend real` only).

## 5. Replay: re-analyze a session without touching hardware again

If the fitter, RFI mask, quality thresholds, or beam model change later,
re-run the analysis on the SAME raw evidence - never re-observes, never
overwrites the original result:

```bash
python almita_align.py hi replay data/alignment/HI-20260918-030000 \
    --bootstrap-iterations 200 --label reanalysis-v2
```

This writes a new, separate
`data/alignment/HI-20260918-030000/analyses/analysis-<timestamp>-reanalysis-v2/`
directory (its own `analysis_config.json` with the exact config used, the
git commit it ran under, `fit_result.json`, `quality.json`,
`analysis_result.json`) and never modifies the original session files.

## 6. Abort / recovery

- **Ctrl-C (SIGINT) during a real scan**: the script catches this,
  restores the original tracking mode via the mount's own readback-
  confirmed path, writes `raw_grid.json` with every point attempted so far
  marked valid/invalid and every remaining point marked "not attempted",
  and exits 5. No fit is attempted on an incomplete/insufficient raster.
  All completed points' raw IQ and evidence are preserved.
- **A GOTO or acquisition failure on one point**: that point is marked
  invalid with a reason, the scan continues to the next point (a single
  bad point never aborts the whole raster).
- **If in doubt about mount state afterward**: check
  `<session_dir>/tracking_after.json` (`"restored": true/false`) and the
  mount's own current tracking mode directly - this runbook does not
  invent a new mount-state check beyond what the session already records.
- **Never** use `deployment set-field`/`set-indoor` as a way to "unblock"
  a run you don't understand the block on - read the actual gate reason
  first (`hardware_test_result.json`'s `reason` field, or the printed
  `BLOCKED` line).

## Repeatability (after a successful first light - see also section on
future SYNC below)

The experiment: **Scan A** (target X) → wait a reasonable interval →
**Scan B** (same target X, same raster geometry) → compare, with **no
SYNC in between**. Both scans use step 3 above unchanged. Comparison uses
`alignment_engine.hi.repeatability.compare_alignment_results()` on the two
sessions' replay/analysis output - see that module's docstring for the
CONSISTENT/MARGINAL/INCONSISTENT/DIFFERENT_REGION verdicts and why a
cross-target difference is a pointing-model diagnostic, not a failure.
This is deliberately not wired into a single CLI command yet - two real
scans have to exist first.

## Future SYNC (not available - policy only)

`evaluate_phase_gate()` in `alignment_engine/hi/sync_policy.py` is the
single place SYNC could ever be allowed, and today it always returns
`sync_allowed=False` because `alignment_phase=FIRST_LIGHT_HI` is
hardcoded at every call site. Moving to `AlignmentPhase.ESTABLISHED_HI`
would still require, all at once: a `REAL_VALIDATED` reference, an
explicit FIELD deployment gate result, `sync_eligibility_verdict ==
"ELIGIBLE"`, and a satisfied `check_repeatability()` result (>=2
independent same-target solutions agreeing within 3-sigma). There is no
button that does this - it is architecture for a decision this project
has not made yet, not a feature waiting to be turned on.
