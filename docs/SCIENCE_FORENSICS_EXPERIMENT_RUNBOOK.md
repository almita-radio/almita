# SCIENCE FORENSICS EXPERIMENT: field runbook

Written for the operator standing at the mount. Machine-readable twin: `examples/science_forensics_experiment.yaml`
(experiment `SCIENCE-FORENSICS-EXP-HI148-01`). Nothing in this file is executed by software: every hardware step is a manual action.

> The next improvement is not a smarter regression. It is a smarter observation.

## 1. What this experiment answers

The ~150 km/s LSRK feature of the 9-point campaign moved 80 channels (-3.2e5 Hz/h) in 4.3 min while the antenna stepped through a
serpentine raster. In that raster time, Dec and capture order were one variable (corr(time, Dec) = 0.95, VIF 11), the feature was
stationary in neither LSRK nor topocentric frequency, and no other campaign shows it. Time and sky cannot be separated by any analysis
of those 9 captures; the new observation separates them physically:

- **Experiment A (fixed sky, repeated time):** the antenna does not move; one position `A` is captured 60 times (~29 min).
  A sky-stationary line drifts in topocentric frequency only by (f0/c) x d(LSRK shift)/dt = about -320 Hz/h. The old drift was
  -3.2e5 Hz/h, a thousand times larger. If the feature is still drifting at fixed sky, it is temporal.
- **Experiment B (different sky, nearly the same time):** the antenna alternates `A x B A x C A ...` (65 captures, ~36 min).
  Every B/C capture has an A capture ~30 s before and ~30 s after, so a temporal drift is interpolated away and only a
  position effect remains. B and C are chosen so that a stationary sky line would move by 22-24 channels (6-7 kHz) between
  positions; a receiver-fixed line by 0.
- **A + B in one session (recommended):** `A x 20, B-sequence, A x 20` (105 captures, ~55 min): the A series before and after
  spans the whole session and A is time-balanced against B/C.

## 2. Positions (from the plan; regenerate for your real start, section 4)

| label | RA (deg / h) | Dec (deg) | note |
|---|---|---|---|
| A | 177.45 / 11.830 | -33.449 | centre of the feature campaign grid: the reference sky |
| B | 177.45 / 11.830 | -28.949 | 4.5 deg north of A |
| C | 182.25 / 12.150 | -35.699 | 4.55 deg from A, 7.9 deg from B |

Predicted (2026-09-21 14:45 UTC, frozen `frame_shift_km_s` helper): LSRK shift A -12091 m/s; B-A +1460 m/s (+23.6 channels, +6919 Hz);
C-A -1353 m/s (-21.9 channels, -6410 Hz). Stationary sky: B is seen 6.9 kHz higher than A, C 6.4 kHz lower. Receiver-fixed: 0.

## 3. Files

- `examples/science_forensics_experiment.yaml`: plan, hypotheses, thresholds, timing, logging requirements, power test, readiness.
- `examples/science_forensics_experiment/{A,B,AB}/mosaic.csv`: `capture.py --csv` plans (real `mosaic.csv` schema plus
  `experiment_label`/`experiment_visit`). Repeated coordinates are distinct rows with distinct `point_number` and `data_filename`.
- Tool: `almita_science_forensics_experiment.py` (`design`, `windows`, `simulate`, `replay`, `analyze`, `validate-csv`).

## 4. Before going to the field (desk, offline)

```
cd ~/almita
# 4.1 when can the whole run stay on ONE hour-angle side? (A, B, C above 30 deg, margin 0.25 h)
./.venv/bin/python almita_science_forensics_experiment.py windows --date <YYYY-MM-DD> --days 3 --minutes 65
# 4.2 regenerate the plan for YOUR start (predicted shifts depend slightly on the date) and copy observer_config next to each CSV
./.venv/bin/python almita_science_forensics_experiment.py design --epoch <YYYY-MM-DDTHH:MM:00+00:00> \
    --out data/experiments/EXP-01/plan.yaml --emit-dir data/experiments/EXP-01 --copy-observer-config
./.venv/bin/python almita_science_forensics_experiment.py validate-csv data/experiments/EXP-01/AB/mosaic.csv
```
Start inside a `windows` line. Two hours later the same positions cross the meridian: `capture.py` would re-block the points by
hour-angle sign and reorder them, and a crossing slew costs 64-85 s. After the run, `analyze` checks that capture order matches time.

## 5. Preflight at the mount (manual)

1. Mount and antenna deployed outdoors, INDI/OnStep healthy, clear sky path for A, B, C (no obstruction below 30 deg altitude).
2. **Instrument identical to the feature campaign** (audited from its `observation_resolved.json`): capture 10 s, settle 2 s, RTL-SDR
   Blog V4 at 1420405752 Hz, 2.4 MS/s, gain 40.2 dB, bias tee as configured (the orchestrator passes no main bias-tee flag), input topology `antenna`, RFI_REF enabled (serial `00000002`,
   gain 25 dB), min altitude 10 deg. Do not touch LNA, cabling or feed between runs; record anything that changed.
3. `python3 capture.py --csv <CSV> ... --preflight-only` (command in the plan under `capture_command_template`). Nothing moves.
4. Log start: Pi clock (`chronyc tracking` or `timedatectl`), SDR and LNA temperature sensors reading, ambient temperature, wind,
   time the SDR stream is opened. The OnStep clock drift finding stays as documented; do not correct it during the run.
5. RFI_REF chain running (second RTL-SDR), same serial and gain.

## 6. Run

```
python3 capture.py --csv data/experiments/EXP-01/AB/mosaic.csv --settle 2.0 --capture 10.0 --sdr-freq 1420405752 --sdr-rate 2400000 \
    --sdr-gain 40.2 --input-topology antenna --min-altitude 10.0 --rfi-ref-enabled --rfi-ref-gain-db 25.0 --rfi-ref-serial 00000002
```
(exactly the arguments the orchestrator used for the feature campaign; `design` writes the same command into the plan.)
Expected: ~29 s per captured point on A, ~33 s when alternating; 105 points = 55 min (p90 59 min). The orchestrator and the
OBSERVE spec cannot express an explicit sequence, which is why `capture.py --csv` is used directly.

- **Stop:** send SIGINT to the capture.py PID only (never SIGTERM/SIGKILL); it stops its own children.
- **Abort now** if: a slew takes > 90 s (hour-angle crossing), altitude gate rejects a point, tracking is not confirmed, RFI_REF or SDR
  drops, or the mount moves unexpectedly. A partial session is still analysable; never restart it in place.
- Do not edit the CSV, do not reorder, do not re-run a failed point inside the same session.

## 7. After the run (desk, offline)

```
python3 almita_reduce.py run data/mosaic/<SESSION_DIR> --output-root data/reduced --velocity-frame lsrk \
    --calibration-profile data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1.json
./.venv/bin/python almita_science_forensics_experiment.py analyze data/reduced/<CAMPAIGN>/<REDUCE_SESSION> \
    --plan-csv data/experiments/EXP-01/AB/mosaic.csv
./.venv/bin/python almita_science_forensics.py run <REDUCE_SESSION> --center 150857 --half-width 5607 --feature-id EXP01 \
    --site-from-observer-config observer_config.json --output-root data/science_forensics     # V1 forensics, for the standard plots
```
Run one heavy job at a time (the Pi has ~no free swap). REDUCE V1 is not modified; per-capture RFI_REF association, temperature and gain
remain in RAW metadata and must be read from the logs of section 5.

Outputs of `analyze` (`data/science_forensics_experiment/<campaign>/`): `experiment_analysis.json` (decision, null-hypothesis
tests with effect size and uncertainty, reference series, matched pairs, global shift, controls, robust morphology per capture) and
`summary.md`.

## 8. Reading the result

| decision | meaning | what it needs |
|---|---|---|
| `SKY_FIXED` | position offsets equal the parameter-free stationary-sky prediction (H0_sky consistent), no drift beyond -320 Hz/h | a sky feature; classify with independent data |
| `RECEIVER_FIXED` | offsets are zero within uncertainty (H0_frequency consistent) | instrument/RFI follow-up |
| `TIME_GLOBAL` | drift with time; the whole-spectrum shift follows the candidate (slope ~1, R2 >= 0.8) | LO/clock/thermal drift of the receiver |
| `TIME_CANDIDATE_ONLY` | drift with time; controls and whole spectrum do not move | source-specific temporal behaviour (RFI, transient) |
| `SKY_GRADIENT` | position dependent but not the stationary-sky pattern | real spatial structure (or pointing) |
| `MIXED_TIME_AND_SPATIAL` | both | sub-analyse A series and offsets separately |
| `STATIONARY_UNDISCRIMINATED` | stationary in time; only one sky position was observed | run experiment B |
| `UNRESOLVED` | too few detections or a non-resolvable prediction | say so; do not interpret |

Every decision carries: H0_time (excess drift, z, total channels over the span, stationarity chi2/dof), H0_sky and H0_frequency
(chi2/dof of the bracketed B/C offsets against prediction and against zero), the global-shift-vs-candidate regression, and a list of
limits. Effect size and uncertainty are always reported next to the label. Difference spectra and shift-and-difference
(`analysis.shift_and_difference`) are diagnostics only and are not used in the decision.

## 9. If the feature is absent

Formal SNR of the tracked line below 5 in most A captures: record it as "feature not reproduced at HA/LST X". That is a result
(consistent with a transient, with an HA/LST-dependent effect, or with RFI of the earlier day). Do not extend the analysis to find it. The
A series still gives: stationarity of the spectrum, the control-line behaviour and the per-capture median level (section 11).
If the operator wants the same LST/HA as the feature campaign (HA of A about -1.45 h), `windows` reports the date/time that reaches it.

## 10. Logging required for interpretation (not in REDUCE Level 1)

RFI_REF running for the whole session (serial, gain), SDR/LNA temperatures at start and end and any excursion, ambient temperature, Pi
clock source and offset at start and end (timezone-aware UTC), the moment the SDR stream opened (warm-up is not assumed), mount-reported
RA/Dec after every GOTO for repeated visits (pointing repeatability: the runbook cannot know it), any change of cabling.

## 11. Negative-background follow-up (same data)

SCIENCE integrated maps are strongly negative; the replay of the old 9 points shows the per-capture median relative intensity going
from -0.0094 to -0.0062 while `baseline_fit_quality_rms_fraction` fell 0.0537 -> 0.0513: both correlate with time (|r| 0.80-0.89) and with the
candidate channel (|r| 0.87-0.89), with n = 9, so they are not separable. In this experiment: per-capture median level vs time on A (drift
= instrument), vs position on B/C (position dependence = sky or pickup), vs `baseline_fit_quality_rms_fraction`. A flat, position-independent
negative level is a fixed REDUCE baseline offset.

## 12. Time budget

Median / p75 / p90 from the real timing of 4541 intervals in 34 campaigns: A 28.8 / 29.6 / 30.1 min; B 36.3 / 38.0 / 40.0 min; A+B 54.8 / 57.0 / 59.3 min
(plus about a minute for the first slew). Alternating sky costs ~4 s more per capture than repeating; slews of <= 7.5 deg cost 22-32 s.

## 13. Field readiness

**READY FOR LIVE PREFLIGHT (plan and analysis).** (Earlier wording said "READY FOR FIELD"; that status is now reserved for after a real, passing `capture.py --preflight-only`. Exact operator commands: `docs/SCIENCE_FORENSICS_FIELD_RUNBOOK.md`.) Software: plan, CSVs, capture-compatibility (validated against `capture.py`'s own preflight rules), REDUCE/SCIENCE identity of
repeated coordinates (tested), analysis (synthetic and replayed real data). Operator prerequisites: mount and antenna deployed outdoors (indoors no real
GOTO), manual `capture.py --csv`, start inside a `windows` result, `--preflight-only` first. Known limits: bracket interpolation is exact for linear drift
and leaves curvature error for strongly non-linear wander (see the power test), the feature may be absent, OBSERVE is not modified.
