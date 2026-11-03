# rtl_tcp as a systemd service

This setup keeps `rtl_tcp` listening only on the local interface
`127.0.0.1:1234` and restarts it automatically if the process exits.

> Since November 2026 `rtl_tcp` runs on the Raspberry Pi 4 capture box and the notebook reaches it through
> `instrument_forwarder` (see [README.md](README.md)). The unit below describes the single-machine setup; on the capture Pi
> `rtl_tcp` must listen on the network interface the notebook reaches, not only on 127.0.0.1. The capture-Pi install script
> on the development branch (`scripts/install_capture_pi.sh`) enables `rtl_tcp.service` there.

## Service unit

Create `/etc/systemd/system/rtl_tcp.service` with this content:

```ini
[Unit]
Description=RTL-SDR TCP server for ALMITA
After=network.target

[Service]
Type=simple
User=stellarmate
Group=stellarmate
ExecStart=/usr/bin/rtl_tcp -a 127.0.0.1 -p 1234 -f 1420405000 -s 2400000 -g 40.2 -T
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Before enabling the unit, close any manually started `rtl_tcp` so that two
processes do not compete for the SDR or for port 1234.

## Enabling it

After creating or changing the unit:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rtl_tcp
sudo systemctl status rtl_tcp
```

`enable --now` makes it start at boot and starts the service right away.

## Logs and checks

Follow the service journal live:

```bash
journalctl -u rtl_tcp -f
```

Check that the server listens on TCP/1234:

```bash
ss -lntp | grep 1234
```

The expected output contains a `LISTEN` socket on `127.0.0.1:1234`. If the
service fails, look in the journal first for USB device access errors, a busy
interface or a port already in use.

## RFI_REF: a disposable secondary SDR (V3) in parallel with MAIN

MAIN (`rtl_tcp.service`, serial `00000001`, `127.0.0.1:1234`; since 2026-10-17 it is an RTL-SDR Blog V3 with its EEPROM
reprogrammed to that serial, because the V4 was retired) is the only authoritative source for Capture. A second RTL-SDR
(V3/R820T, serial `00000002`) can be used in parallel as an RFI reference ("RFI_REF"), but **never** as part of
`rtl_tcp.service` or of the science path: it is launched as a separate, temporary `rtl_tcp` process on
`127.0.0.1:1235`, and a failure, delay or crash in it must never be able to block, pause or alter MAIN's acquisition.

This architecture was validated on hardware with
[`dual_sdr_benchmark.py`](../../dual_sdr_benchmark.py):

```bash
python3 dual_sdr_benchmark.py --duration 180
```

The script never restarts or changes `rtl_tcp.service`. It launches RFI_REF as its own subprocess and checks by PID (via
`ss -lntp`) that the `127.0.0.1:1235` socket really belongs to the subprocess it launched, so it never consumes a
pre-existing manual `rtl_tcp` on that port by mistake. It isolates any RFI_REF-side failure from MAIN with
`asyncio.gather(..., return_exceptions=True)`. RFI_REF's quick-look FFT runs in a separate thread so it never blocks the
loop that drains MAIN's socket. RFI_REF's full IQ is deliberately not kept. It is a reference side-car, not a source of
science data, so a light quick-look FFT (~5% of the blocks, no IQ saved) is enough to characterise RFI without adding
I/O load.

Validated result (a 180 s baseline run + a 180 s dual run; MAIN at 1420405000 Hz / 2.4 MS/s / gain 40.2 dB / Bias-T ON;
RFI_REF at 1420405000 Hz / 2.4 MS/s / gain 25 dB):

- MAIN dual/baseline throughput ratio: 0.99999
- losses/errors/discontinuities on MAIN: 0
- USB resets: 0
- mean system CPU during dual: 2.75%
- RAM used: 23.3%
- RFI_REF quick-look FFT duty: ~5% of the blocks
- losses on RFI_REF: 0

The full evidence of each run (per-block CSV, resource sampling, logs, JSON) is kept under
`data/dual_sdr_bench/<run_id>/` and is not versioned in git (see `.gitignore`): it is runtime evidence, not a code
artefact.

## RFI_REF inside Capture: an optional, disposable side-car

The standalone benchmark above validates the MAIN+RFI_REF rtl_tcp pair on its own.
[`rfi_monitor.py`](../../rfi_monitor.py) (`RFIReferenceMonitor`) brings the same architecture into `capture.py` as a
real field side-car:
- socket ownership checked by PID;
- its own subprocess, never managed by `rtl_tcp.service`;
- the quick-look FFT in a separate thread;
- no IQ kept.

**Non-negotiable architecture rule:** MAIN is the only authoritative science source. RFI_REF is optional and
disposable. None of these in RFI_REF may ever block, pause or alter MAIN, or abort the session:
- a failure, crash or bind error;
- a missing device;
- processing lag or malformed output.

"Alter MAIN" covers its gain, `rtl_tcp.service`, the science HDF5 and the mount/GOTO behaviour. RFI_REF is a diagnostic
monitor, **not** an active interference canceller: at this stage it is neither subtracted from nor mixed with MAIN's IQ.

Enabling it (disabled by default, so existing behaviour is unchanged):

```bash
python3 capture.py --csv <plan.csv> --settle 5 --capture 10 \
  --rfi-ref-enabled --rfi-ref-gain-db 25.0 --runtime-dir data/runtime
```

`--rfi-ref-gain-db` is independent of `--sdr-gain` (MAIN's gain is never reused implicitly). `--rfi-ref-port`
(default `1235`) and `--rfi-ref-serial` (default `00000002`) can also be set. RFI_REF's frequency and sample rate always
copy MAIN's (`--sdr-freq`/`--sdr-rate`), so they match exactly by design.

**Lifecycle.**
- Capture starts the side-car as a background task (`asyncio.create_task`, never blocking) right after MAIN's SDR is
  connected.
- It stops it in the `finally` of `execute_observation_plan`, which covers every exit: completed, paused or aborted
  session, `KeyboardInterrupt`.

**Status.**
- RFI_REF's state is published in `data/runtime/rfi_ref_status.json`, tied to the same canonical `session_id` Capture
  already uses (never a second session identity).
- The watcher (`almita_console_watcher.py`) folds it into `almita_status.json` under the key `rfi_ref`.
- A missing file, one from an old session, or one without the field (sessions from before this integration) is always
  read as `DISABLED`, never as an error.

**Console.** The local console (`console/`) adds an **RFI REFERENCE** panel showing state, serial, gain, duty, occupancy,
clipping, peak and last update. An RFI_REF failure never changes MAIN's session state to `ABORTED`.

### "SATANIC" hardware validation

Besides the standalone benchmark, an adversarial hardware validation was run with the real production classes (MAIN's
`SDRCapture` and `RFIReferenceMonitor`) against the real `rtl_tcp.service` and a real RFI_REF on `127.0.0.1:1235`. It was
designed on purpose to break RFI_REF while preserving MAIN:

- **A (normal):** MAIN and RFI_REF together. RFI_REF `RUNNING`, 0% clipping, every MAIN capture valid.
- **B (kill RFI_REF mid-capture), the mandatory criterion:** `SIGKILL` was sent only to RFI_REF's own subprocess while
  MAIN was actively capturing.
  - MAIN completed its 6 captures with no losses or discontinuities.
  - RFI_REF went to `FAILED` with the error recorded.
  - Zero USB resets; `rtl_tcp.service` was never touched.
- **C (port 1235 taken by an unrelated process):** RFI_REF detected the foreign ownership by PID, never connected to it
  or killed it, and stayed `UNAVAILABLE`. MAIN completed its session normally.
- **D (V3 missing, simulated with a non-existent serial):** `rtl_tcp` refused any fallback to another device ("No
  matching devices found"). RFI_REF stayed `FAILED` with `rtl_tcp`'s real diagnosis; MAIN was not affected.
- **E (backpressure / slow FFT):** with an artificial FFT delay (a test-only parameter, via
  `ALMITA_RFI_TEST_FFT_DELAY_S`), RFI_REF dropped its own FFT work (`dropped` blocks) instead of falling behind. MAIN kept
  normal throughput.
- **F (full field-like load):** MAIN and RFI_REF ran with `almita_console_watcher.py` and `almita_console_server.py` for
  ~150 s. MAIN had no losses, and `rfi_ref` showed up in `almita_status.json`.

In every phase:
- `rtl_tcp.service` was never restarted or changed;
- no unrelated process was killed;
- the only processes ended were PIDs that RFI_REF had launched itself.

The full evidence (JSON per phase, MAIN's HDF5, logs) is kept under `data/rfi_ref_satanic/<run_id>/` and is not
versioned in git.

**Scope note.** This hardware validation ran at the SDR/software level: the real `SDRCapture` and `RFIReferenceMonitor`
classes on the real MAIN and RFI_REF hardware, without the point loop with INDI and the mount. Integration with the real
mount (GOTO/tracking) is covered by the offline integration tests in `test_capture_rfi_ref_integration.py`, which do run
the whole lifecycle of `execute_observation_plan()` with a simulated telescope.

## ANTENNA B: three products, none of them a fake sky map

RFI_REF (ANTENNA B) is a fixed reference antenna: it never reproduces MAIN's (ANTENNA A's) pointing geometry. So it
offers three products parallel to MAIN's, honest about what they really are:

1. **RFI Spectrum** (`data/runtime/rfi_ref_spectrum.json`): 256 bins, max-pooled from the same ~5%-duty FFT (never a
   second FFT), frequency axis in absolute Hz, power in dBFS.
2. **RFI Waterfall** (`data/runtime/rfi_ref_waterfall.json`). It reuses the 256-bin array already computed for the
   spectrum at each publish tick. The tick is ~2 s by default, independent of the FFT and always slower than it.
   - It keeps a bounded window (`waterfall_max_rows`, default 120 rows ≈ 4 min) through `deque(maxlen=...)`.
   - Each row is `{"utc", "power_dbfs"}`.
3. **RFI Occupancy Map** (`rfi_occupancy_map.json`/`.png`, served next to ANTENNA A's `quicklook_map.json` under
   `quicklook_products/`). It is **not a sky map**: it overlays an RFI contamination diagnostic on MAIN's native grid
   geometry.
   - Built by `rfi_occupancy_map.py`, which reuses `quicklook_map.load_native_grid_geometry`/`read_cell_status` without
     changing them.
   - For each successful MAIN point, it matches by timestamp overlap (`start_time`/`end_time` from `mosaic.csv`) the
     samples of `data/runtime/rfi_ref_history.json` that fall inside that window.
   - That history is a bounded list of `{utc, occupancy_fraction, clipping_fraction, peak_dbfs}`, on the same publish
     tick as the waterfall.
   - There is no interpolation and no invented cell: a point with no overlapping RFI samples stays absent (NaN/`null` in
     the JSON, never a false 0).
   - It is built by `quicklook_live.py` (an existing, decoupled process) at each `scan_once()`. Never by capture.py, and
     never under a second session identity.

All three products carry the canonical `session_id` and are dropped if it does not match the current session (the same
rule as the already validated spectrum). While `RUNNING`/`DEGRADED` they must also be recent. Once `STOPPED`, the last
valid product of that session is kept for later inspection, whatever its age.

**Real hardware validation.**
- Spectrum and waterfall were run against the real V3, without moving the mount:
  - 256 bins, 10 waterfall rows (correctly bounded), a bounded history;
  - the same frequency array in spectrum and waterfall, which proves the reuse without a second FFT;
  - a measured FFT duty of ~4.9%, stable CPU/RAM, zero USB resets and `rtl_tcp.service` untouched.
- The occupancy map was validated with the **real** geometry and timing of an earlier successful GOTO session (9/9 real
  points). The RFI history was realistic, in the same range of values measured on real hardware, and placed inside
  those real time windows.
  - Result: 9/9 cells observed and the PNG generated correctly.
  - It ran the full production path (`QuicklookLive._update_rfi_occupancy_map`) without reimplementing or mocking the
    logic.
