# E2E BLOCKER FOUND

- Stage: Phase 2 — Grid planning / read-only current-mount status.
- Component: `mount_control.py` operational CLI.
- Command:

  ```bash
  /home/stellarmate/almita/.venv/bin/python mount_control.py --status --host localhost --port 7624 --device 'LX200 OnStep'
  ```

- Expected: establish the existing INDI connection and report the current RA/DEC without sending a mount command, so a mechanically cleared indoor 3x3 grid can be planned.
- Actual: `RuntimeWarning: coroutine 'INDITelescopeControl.connect' was never awaited`; then `get_coordinates()` fails with `RuntimeError: No conectado`.
- Relevant locations: `mount_control.py:119` and `indi_telescope_control.py:533`.
- Impact: no trustworthy current coordinate is available through the operational status entrypoint. The required safe grid centre/visibility/meridian assessment cannot be performed, so no grid, GOTO, capture, or physical phase was started.
- Hypothesis: `mount_control.py` calls the asynchronous `INDITelescopeControl.connect()` as if it were synchronous.
- Data preserved: YES. The Phase 0/1 snapshot and this blocker report are under this E2E directory; no HDF5 was created.
- Hardware safe: YES. No GOTO, SYNC, tracking change, SDR reconfiguration, capture, or service-state change was issued.

## Raw result

```text
/home/stellarmate/almita/mount_control.py:119: RuntimeWarning: coroutine 'INDITelescopeControl.connect' was never awaited
[ERROR] Error al leer coordenadas: No conectado
RuntimeError: No conectado
```
