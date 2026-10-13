# SCIENCE FORENSICS FIELD RUNBOOK

Operator pack for the experiment designed in `docs/SCIENCE_FORENSICS_EXPERIMENT_RUNBOOK.md` (plan `examples/science_forensics_experiment.yaml`,
commit `02b8e5a`). The plan and the three CSVs are **official, immutable inputs**: this pack never regenerates or edits them.

**Status of this pack: READY FOR LIVE PREFLIGHT.** It becomes READY FOR FIELD only when `status` says so after a real `preflight` PASS
(fresh, same plan/CSV/observer_config/commit). Nothing in this pack has run against hardware. The plan file's own wording "READY FOR FIELD"
(section 13 of the experiment runbook) is superseded by this rule.

Everything is run from the repository root:

```
cd ~/almita
S="./.venv/bin/python scripts/science_forensics_field.py"
```

`$S` never replaces `capture.py`; it prepares, checks, calls it, and records evidence. Global options (`--json`, `--plan`) go **before** the subcommand.

## 1. The seven steps (identical for A, B, AB; replace `B` by `A` or `AB`)

```
$S status B
$S precheck B
$S preflight B --print-only
$S preflight B
$S run B --dry-run
$S run B
CID=$(ls -1dt data/science_forensics_field/FORENSICS-B-* | head -1 | xargs basename); echo $CID
$S postcheck $CID
./.venv/bin/python almita_reduce.py run data/mosaic/$CID --output-root data/reduced --velocity-frame lsrk \
    --calibration-profile data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.json
$S postcheck $CID --quick
$S analyze $CID
```

There is no default experiment: `A`, `B` or `AB` must be typed. A successful `preflight` **never** starts an observation; `run` is a separate,
explicit command that asks you to type the experiment name (or pass `--yes`).

### A only (fixed sky, ~29 min, 60 captures)
```
$S status A
$S precheck A
$S preflight A --print-only
$S preflight A
$S run A --dry-run
$S run A
CID=$(ls -1dt data/science_forensics_field/FORENSICS-A-* | head -1 | xargs basename); $S postcheck $CID
```

### B only (alternating A/B/C, ~36 min, 65 captures)
```
$S status B
$S precheck B
$S preflight B --print-only
$S preflight B
$S run B --dry-run
$S run B
CID=$(ls -1dt data/science_forensics_field/FORENSICS-B-* | head -1 | xargs basename); $S postcheck $CID
```

### AB (A x20, B, A x20, ~55 min, 105 captures; recommended)
```
$S status AB
$S precheck AB
$S preflight AB --print-only
$S preflight AB
$S run AB --dry-run
$S run AB
CID=$(ls -1dt data/science_forensics_field/FORENSICS-AB-* | head -1 | xargs basename); $S postcheck $CID
```
Then, for any of them: the `almita_reduce.py run` command above, `$S postcheck $CID --quick`, `$S analyze $CID`.

## 2. What each command does

| command | effect |
|---|---|
| `status [A\|B\|AB]` | read-only: git commit/branch, plan path, UTC, planned-geometry window (and the next start windows), MAIN rtl_tcp `:1234` and INDI `:7624` listening (passive `/proc/net/tcp`, no connection), `rtl_tcp.service`, running `capture.py`, stale sessions in `data/IQ/session.csv`, existing campaigns, frozen-module differences, effective field status |
| `precheck EXP` | read-only: interpreter/venv, official CSV vs the plan (sha256, columns, unique IDs, sequence and A/B/C counts, repeated coordinates kept, coordinates = plan positions, all rows `planned`), disk, writable destinations, endpoint availability (`--offline` skips the endpoints for desk use) |
| `preflight EXP` | runs the **real** `capture.py --preflight-only --debug` on a per-preflight copy of the CSV (`data/science_forensics_field/_preflight/EXP/<UTC>/`); persists stdout, stderr, exit code, timestamps, git commit, plan/CSV/observer_config sha256; result is **PASS** or **BLOCKED** with the exact reasons. `--print-only` shows the command and executes nothing |
| `run EXP --dry-run` | prints the exact `capture.py` command, campaign id, sequence summary, and every blocker; creates nothing, touches nothing |
| `run EXP` | live: needs precheck PASS, the planned-geometry window, a fresh matching PASS preflight (max 120 min old), no other `capture.py`, unchanged frozen modules, no id conflict; asks you to type `EXP`; then calls the existing `capture.py` |
| `postcheck CID [--quick]` | read-only: counts, files, zero-byte/invalid HDF5 (metadata only, IQ never read), timestamps, order, gaps, metadata consistency; **COMPLETE / PARTIAL / FAIL**; `--quick` adds the field diagnostic (needs REDUCE Level 1) |
| `analyze CID` | REDUCE Level 1 -> existing experiment/forensics machinery with the actual capture timestamps; prints the statements below. Without a REDUCE session it prints the REDUCE command; `--run-reduce` runs it for you |

`preflight` talks to the MAIN `rtl_tcp` exactly as a real run does (capture.py configures it with the plan's frequency/rate/gain) and reads the INDI
mount coordinates; it sends no GOTO, no SYNC, no tracking command and captures no IQ. The wrapper itself sets nothing.

## 3. Campaign identity and where things go

Campaign id: `FORENSICS-<A|B|AB>-<UTC>Z`, e.g. `FORENSICS-B-20260921T174500Z`.

```
data/mosaic/<CID>/                     capture.py's campaign directory (mosaic.csv run copy, observer_config.json copy, data/iq/...)
data/science_forensics_field/<CID>/    manifest.json, inputs/ (read-only copies of plan, official CSV, observer_config), logs/,
                                       postcheck.json, analysis/ (experiment_analysis.json, summary.md)
data/science_forensics_field/_preflight/<EXP>/<UTC>/   preflight.json, stdout.txt, stderr.txt, prepared CSV
```
`capture.py` rewrites its CSV in place and names its sessions from `session_name`, so it runs on a **run copy** that differs from the official CSV only in
`session_name` and the prefix of `data_filename` (verified and recorded as `csv_run_columns_changed_vs_official`). The official CSV keeps its sha256 (recorded as
`csv_sha256`, checked against the plan). None of these paths is staged in git.

`manifest.json` fields: `campaign_id, experiment, plan, plan_sha256, csv, csv_sha256, csv_run, csv_run_sha256_initial, observer_config_sha256, git_commit, branch,
operator_command, capture_argv, start_utc, end_utc, capture_exit, status, preflight_result, preflight_report, capture_count_expected, capture_count_actual,
analysis_session`. Once a run starts, the plan YAML, the CSV and the observer_config recorded there are immutable provenance (read-only copies in `inputs/`);
`analyze` warns if the copy no longer matches `plan_sha256`.

## 4. Time window and geometry

`status` and `run` check that A (and B, C) stay above 30 deg and on **one** hour-angle side for the whole run (p90 duration + 5 min) starting now. Otherwise:
`BLOCKED FOR PLANNED GEOMETRY` (capture.py re-blocks by hour-angle sign at its own start and would silently reorder the interleaved sequence; a crossing slew
costs 64-85 s). Positions are never changed automatically, and there is no override for a live run.

If the season has moved so far that the sky-shift contrast between positions shrinks (< 10 channels blocks; < 75 % of the plan recommends a redesign), `status`
prints the command that writes a **new, separate** plan and CSVs (`data/experiments/EXP-<YYYYMMDD>/`); the official files stay as they are:
```
./.venv/bin/python almita_science_forensics_experiment.py design --epoch <YYYY-MM-DDTHH:MM:00+00:00> \
    --out data/experiments/EXP-<YYYYMMDD>/plan.yaml --emit-dir data/experiments/EXP-<YYYYMMDD> --copy-observer-config
$S --plan data/experiments/EXP-<YYYYMMDD>/plan.yaml status B
```
(the new plan's CSVs are referenced by their own path and sha256 in it.)

## 5. During the run

Live output of `capture.py` is shown and also written to `data/science_forensics_field/<CID>/logs/capture_stdout.log` and `capture_stderr.log`.

**Stop safely:** press `Ctrl+C` **once**. The wrapper forwards a single SIGINT to `capture.py` (it runs in its own session) and waits for it to close its files and
stop tracking. A second `Ctrl+C` only prints a reminder. Never `kill -9`, never SIGTERM, never delete the campaign. From another terminal:
`kill -INT $(pgrep -f "capture.py --csv .*$CID")`.

Abort criteria: a slew > 90 s, altitude gate rejects a point, tracking not confirmed, MAIN SDR or RFI_REF drops, unexpected mount motion. A partial campaign is
evidence: keep it, run `postcheck` (it reports `PARTIAL`), and start a **new** campaign rather than `capture.py --resume` (a resume would put a long gap inside the
interleaved series).

## 6. Postcheck outcomes

`COMPLETE` (all planned captures valid), `PARTIAL` (valid data but fewer than planned: interrupted, missing, zero-byte or invalid files), `FAIL` (no valid capture or
no manifest/CSV). It also reports: A/B/C counts vs plan, repeated A present, timestamps strictly increasing, capture order equal to the plan (a critical issue if
`capture.py` re-blocked by hour angle), campaign duration, large gaps, metadata consistency with the plan (frequency, rate, gain), and the field metadata that
exists (LNA/SDR temperature ranges, gain, frequency, rate, bias-T, mount state) with everything absent marked `MISSING`: ambient temperature and the SDR serial are
not in the capture files and must be logged by hand (experiment runbook section 10).

## 7. Analysis output

`analyze` prints, in this order: same-sky temporal drift (A) against the sky-fixed expectation computed from the **actual** timestamps and pointing; cross-sky
matched pairs (A-B, A-C) against the sky-fixed prediction (per pair, at the actual time of each capture) and against receiver-fixed 0; candidate shift vs global
shift and their ratio (if the global shift is not significant it says so: e.g. "candidate moved -80 channels; the whole spectrum moved 0.0 +/- 0.1 channels; ratio
undefined"; no cause is inferred); frame coherence (forensics V1); control feature behaviour (or that none exists); known-spur position stability from the mask
geometry only; and the classification. Vocabulary: `CONSISTENT WITH`, `INCONSISTENT WITH`, `UNRESOLVED` (chi2/dof < 4, >= 9, otherwise). Every prediction uses the
capture timestamps in Level 1; a cross-check of REDUCE's LSRK shift against the frozen helper at those timestamps is reported (`shift_crosscheck_ok`).

## 8. Indoors: INDOOR_MODE

While the antenna/mount are indoors, `export INDOOR_MODE=1`: `preflight` and any live `run` are refused (dry-run and `preflight --print-only` still work), so a preflight
recorded indoors cannot unlock READY FOR FIELD. Use `scripts/science_forensics_bench.py` (`docs/SCIENCE_FORENSICS_INDOOR_BENCH.md`) for the indoor infrastructure checks.
`unset INDOOR_MODE` outdoors before the real `preflight`.

## 9. What this pack will not do

Change `capture.py`, REDUCE, SCIENCE, forensics V1, the OBSERVE scheduler/orchestrator, systemd, `rtl_tcp`, gain, centre frequency or `observer_config.json`;
move the mount outside an explicit `run`; SYNC; delete or clean data; push. `data/IQ/session.csv` is rewritten by `capture.py` during a run (it already shows as
modified in git): do not stage it.
