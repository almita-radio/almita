# E2E BLOCKER FOUND

- Stage: Phase 3 — Calibration Profile from the contemporaneous 50-ohm baseline.
- Component: `build_calibration_foundation_v1.py` operational CLI.
- Command shape required by the CLI:

  ```bash
  /home/stellarmate/almita/.venv/bin/python build_calibration_foundation_v1.py \
    --output-dir <output> --reference <50ohm_a.h5> <50ohm_b.h5> \
    --antenna <antenna_capture.h5>
  ```

- Expected: create the relative 50-ohm Calibration Profile from the two confirmed baseline references, with antenna validation deferred to the later antenna/session phases.
- Actual: `--antenna` is required by the only operational CLI. At this stage the physical input is still the confirmed 50-ohm load, so no valid antenna capture exists.
- Why execution was not forced: supplying a 50-ohm reference as `--antenna` would falsely label the validation and violate the E2E source/topology traceability requirement. Calling the internal Python API instead would bypass the operational entrypoint prohibited by the E2E procedure.
- Impact: Phase 3 cannot create its requested Calibration Profile through the real CLI before the required antenna transition. The E2E sequence must stop pending a CLI mode that builds a reference-only profile and defers antenna validation.
- Hypothesis: the CLI combines two distinct operations: reference-profile construction and optional antenna validation.
- Data preserved: YES. Two valid contemporaneous reference HDF5 files are preserved under `baseline/`.
- Hardware safe: YES. The mount remained immobile; both SDR client connections closed normally; no GOTO, SYNC, tracking change, or alignment action occurred.

## Valid baseline evidence

- `50ohm_a.h5`: 4,800,000 complex samples, 2.0 s, manual gain 40.2 dB, AGC false, clipping 0.0.
- `50ohm_b.h5`: 4,800,000 complex samples, 2.0 s, manual gain 40.2 dB, AGC false, clipping 0.0.
- Both carry `50_OHM_TO_LNA_FILTER_CABLING_TO_RTL_SDR`, `bias_t_state=ON`, and `capture_status=success`.
