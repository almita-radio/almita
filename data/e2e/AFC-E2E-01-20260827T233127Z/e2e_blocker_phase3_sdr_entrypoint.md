# E2E BLOCKER FOUND

- Stage: Phase 3 — Calibration / 50-ohm baseline entrypoint discovery.
- Component: `sdr_capture.py`.
- Command:

  ```bash
  /home/stellarmate/almita/.venv/bin/python sdr_capture.py --help
  ```

- Expected: print CLI help only; no SDR connection, configuration, capture, or output.
- Actual: the script did not implement help parsing and executed its built-in comparison/demo flow. It connected to `rtl_tcp`, logged `SDR CONFIG INIT f=1420405752 sr=2400000 manual_gain=0 gain=auto`, and captured 2,400,000 samples for 1.0 second.
- Contract violation: Phase 3 requires fixed manual gain 40.2 dB and AGC off. The accidental demo used automatic gain and therefore is not an AFC-E2E-01 baseline product.
- Impact: there is no safe operational CLI for selecting a controlled 50-ohm baseline capture from this entrypoint. The expected E2E baseline HDF5/metadata/calibration-profile sequence has not been started.
- Hypothesis: `sdr_capture.py` has executable test/demo code under its module entrypoint rather than an argument-parsed operational CLI.
- Data preserved: YES. The E2E grid/preflight evidence is preserved. The accidental demo output is outside the E2E evidence directory and is explicitly excluded from E2E use.
- Hardware safe: YES mechanically. No mount command, GOTO, SYNC, tracking change, or alignment action was issued; the `rtl_tcp` client disconnected after the one-second demo capture. The SDR configuration must be explicitly re-established at the next authorized attempt because it was not left in the required verified manual-gain baseline state.

## Raw relevant result

```text
SDR CONFIG INIT f=1420405752 sr=2400000 manual_gain=0 gain=auto
Capturing 2,400,000 samples (1.0s) via network...
Capture complete: 1.01s transfer + 0.18s write
rtl_tcp connection closed
```
