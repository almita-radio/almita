# AFC-E2E-01 — Phase 0/1 preflight snapshot

- UTC: 2026-08-27T23:31:27Z
- Environment: INDOOR — DEPARTMENT. No astronomical interpretation is permitted.
- Release: `86dba5bfd22e2f18a5e6ebf9621d0cac0db4ba7b` (`afc-00-go-20260827`).
- Host: `stellarmate`; Python: `3.11.2`.
- Clock: synchronized; NTP active; timezone UTC.
- Storage: 367 GiB available on the working filesystem.

## Pre-existing runtime state (not changed)

- `rtl_tcp.service`: active, listener `127.0.0.1:1234`, PID 65101.
- INDI server: active process on port 7624 with `indi_lx200_OnStep` loaded.
- `rtl_test -t`: RTL-SDR Blog V4 is visible but busy (`usb_claim_interface error -6`), consistent with the active `rtl_tcp` owner.

## Effective operational configuration

- Center frequency: 1,420,405,752 Hz.
- Sample rate: 2,400,000 S/s.
- Requested tuner gain: 40.2 dB fixed/manual.
- AGC: off / not used by the capture contract.
- Bias-T: enabled by the capture contract for the LNA topology.
- Input topology for sky capture: `antenna` (requires operator confirmation before use).
- Beam FWHM: 20.0 degrees.
- Beam sampling fraction: 0.3333333333; nominal spacing 6.666666666 degrees.
- Default settle/capture values in observer configuration: 1.0 s / 10.0 s. The capture CLI requires explicit values.
- Capture preflight minimum altitude: configuration value or 30 degrees by default.
- Tracking confirmation timeout: 5 s by default.
- Capture storage is session-scoped under the selected grid/session path.
- IERS: offline configuration is applied by `astropy_offline.py` from the operational entrypoints.

## Safety gate pending

No 3x3 grid has been generated and no mount command has been sent. The indoor mechanical envelope, walls, ceiling, furniture, cable-clearance limits, and an approved center/maximum slew radius are not encoded in `observer_config.json`. A safe 9-point region must be supplied or explicitly approved before Phase 2.
