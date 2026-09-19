# REDUCE: First Runbook

All commands below are 100% offline (no hardware, no INDI, no rtl_tcp, no
network) and safe to run against real historical data - they never write
inside the raw campaign directory.

## 1. inspect - discover, write nothing

```
python3 almita_reduce.py inspect data/mosaic/<CAMPAIGN_DIR>
```

Reports campaign id, grid, observer metadata, and how many points were
discovered vs accepted (with a reason for every rejected point - e.g. a
declared `capture_status != success`, or no matching HDF5 file found for
the declared filename stem).

## 2. plan - preflight checks, write nothing

```
python3 almita_reduce.py plan data/mosaic/<CAMPAIGN_DIR> \
    --output-root data/reduced \
    --velocity-frame lsrk \
    --calibration-profile data/calibration/<PROFILE_STEM>   # optional
```

Every check is offline-only (filesystem/metadata reads). Exit code 1 if
any check fails; the failing check's name and detail are always printed.

## 3. run - the real thing

```
python3 almita_reduce.py run data/mosaic/<CAMPAIGN_DIR> \
    --output-root data/reduced \
    --velocity-frame lsrk \
    --calibration-profile data/calibration/<PROFILE_STEM>   # optional
```

Runs preflight first (refuses to start if blocked), then the full
INGEST -> ... -> PERSIST pipeline per point. Prints the session id,
per-status point counts, quality counts, runtime, and output directory.
Exit code 0 for `COMPLETED`/`PARTIAL`, 1 for `FAILED` or `BLOCKED`.

Without `--calibration-profile`, every point's `calibration_level` is
`UNCALIBRATED` (quality `WARNING` at best, never `BAD` for that reason
alone). With a compatible profile, `calibration_level` becomes
`RELATIVE`.

## 4. replay - reprocess history, offline

```
python3 almita_reduce.py replay data/reduced/<CAMPAIGN_ID>/<REDUCE_SESSION_ID> \
    --output-root data/reduced
```

Reads that session's `manifest.json` for its `source_campaign_root`,
re-discovers the campaign fresh, and re-runs the full pipeline into a
**new** session id. Never overwrites the session being replayed.

## 5. compare - regression only

```
python3 almita_reduce.py compare \
    data/reduced/<CAMPAIGN_ID>/<SESSION_A> \
    data/reduced/<CAMPAIGN_ID>/<SESSION_B>
```

Reports whether the two sessions share the same source campaign/config,
their point counts, quality counts, and a per-point RMS difference. Not
an astrophysical comparison.

## 6. status

```
python3 almita_reduce.py status
```

Prints `pipeline_version`/`reduce_schema_version` - no campaign required.

## Reading the output

```
data/reduced/<CAMPAIGN_ID>/<REDUCE_SESSION_ID>/
    manifest.json           <- start here: status, counts, per-point outcomes
    config.json
    provenance.json         <- "how exactly was this produced"
    points/<point_index>/master_spectrum.{h5,json}
    logs/{session.log,events.jsonl}
```

A quick look at one point:

```python
from reduce_engine.storage import load_master_spectrum
metadata, arrays = load_master_spectrum("data/reduced/<CAMPAIGN_ID>/<SESSION_ID>/points/1")
print(metadata["quality"])
print(arrays["frequency_hz"].shape, arrays["relative_intensity"].shape)
```

## Validating REDUCE itself

```
python3 -m pytest test_reduce_models.py test_reduce_spectral_and_masks.py \
    test_reduce_baseline_and_velocity.py test_reduce_averaging.py \
    test_reduce_calibration_and_quality.py test_reduce_ingest_and_resample.py \
    test_reduce_rfi_ref_and_uncertainty.py test_reduce_golden_synthetic.py \
    test_reduce_adversarial.py test_reduce_real_historical.py -q
```

`test_reduce_real_historical.py` requires the real fixture campaign
`data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16/` to be present
(it is skipped otherwise, never fails a CI run for a missing local
dataset).
