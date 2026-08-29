# E2E BLOCKER FOUND

- Stage: Phase 4 — Antenna / Alignment.
- Component: `alignment.py` operational CLI.
- Command:

  ```bash
  /home/stellarmate/almita/.venv/bin/python alignment.py \
    --reference auto --dry-run --no-sync \
    --output-dir data/e2e/AFC-E2E-01-20260827T233127Z/alignment_dry_run \
    --observer-config observer_config.json \
    --host localhost --port 7624 --device 'LX200 OnStep' \
    --sdr-host 127.0.0.1 --sdr-port 1234
  ```

- Expected: an operational alignment mode suitable for the explicitly indoor, non-astronomical AFC-E2E-01 context, or an explicit refusal before physical movement.
- Actual: `--reference auto` selected `HI` as its fallback reference and proposed catalog-derived center RA 15 h, DEC -68.0488 deg. Its real execution would perform a multiscale physical GOTO/capture ensemble and analyze HI metrics/template structure.
- Why execution stopped: the E2E scope explicitly forbids treating indoor signals as HI or using HI/Sun observational metrics. The dry-run's PASS means only that it could resolve a catalog template; it is not authorization for physical alignment and must not be interpreted scientifically.
- Impact: no permitted indoor alignment procedure exists in the current operational CLI. Fases 5–13 cannot proceed because their first physical GOTO depends on successful alignment.
- Hypothesis: the alignment entrypoint does not offer an instrumental/indoor no-reference mode that preserves mount safety checks while avoiding Sun/HI inference.
- Data preserved: YES. The 50-ohm baselines, reference-only Calibration Profile, grid, and dry-run result are under this E2E directory.
- Hardware safe: YES. The command used `--dry-run --no-sync`; it created no SDR or telescope object, issued no GOTO/SYNC/tracking change, and performed no capture.

## Dry-run evidence

```text
Reference      HI
Physical mask  NOT inferred
SYNC           NO
Result         PASS (DRY RUN)
```

The complete non-observational dry-run result is `alignment_dry_run/alignment_result.json`.
