# SCIENCE: First Runbook (V1)

Real commands, run against a real REDUCE session
(`data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411`,
produced for this pass's own audit). No hardware anywhere below.

## 0. Prerequisite: a REDUCE session must already exist

```
./.venv/bin/python almita_reduce.py run \
  "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16" \
  --output-root data/reduced \
  --calibration-profile "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1"
```

## 1. Inspect (read-only)

```
./.venv/bin/python almita_science.py inspect \
  "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411"
```

Expected: `science_contract: OK`, 9 points, RA/DEC extent, velocity
available, calibration level `RELATIVE`.

## 2. Plan (no writes)

```
./.venv/bin/python almita_science.py plan \
  "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411" \
  --beam-fwhm-deg 1.5 --output-root data/science
```

`--beam-fwhm-deg 1.5` is an OPERATOR-PROVIDED value (this campaign's
observation grid spacing, read by the operator from
`grid_metadata.json`; SCIENCE never discovers it). A beam is mandatory:
without `--beam-fwhm-deg` or `--beam-from-observer-config [PATH]` the CLI
refuses, because a wrong beam silently changes the grid size (a real
4.7 GB OOM-kill happened this way). `plan` prints estimated RAM, disk and
the machine's MemAvailable.

Expected: grid shape, beam summary, memory/disk estimate, all checks OK.

## 3. Run

```
./.venv/bin/python almita_science.py run \
  "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411" \
  --beam-fwhm-deg 1.5 --output-root data/science
```

Real measured result (second pass, after hardening): `SCIENCE COMPLETED`,
data completeness `COMPLETE`, 21x21 grid, 8192 ascending velocity channels,
quality `GOOD` with limitations `PROVISIONAL_BEAM_MODEL` and
`VELOCITY_RESAMPLED` (limitations do not downgrade the state), 2.3 s,
peak RSS 282 MB, 119 MB output. The run prints the QC summary (input
REDUCE, campaign, points used/excluded, beam, grid, cube, velocity range,
resampling, window, valid map fraction, median uncertainty, runtime, RSS,
output). See `docs/SCIENCE_ACCEPTANCE.md` for the three real campaigns.

## 4. Validate output integrity

```
./.venv/bin/python almita_science.py validate <the SCIENCE_SESSION_ID directory `run` printed>
```

Expected: `output_integrity: OK`.

## 5. QC plots (optional - lazy matplotlib import, section 65)

```python
from science_engine.qc_plots import (
    plot_science_map, plot_uncertainty_map, plot_coverage_map,
    plot_channel_map, plot_representative_spectrum,
)
session = "<the SCIENCE_SESSION_ID directory>"
plot_science_map(session, "integrated_relative_intensity", f"{session}/qc/plots/integrated_map.png")
plot_uncertainty_map(session, "integrated_relative_intensity", f"{session}/qc/plots/uncertainty_map.png")
plot_coverage_map(session, f"{session}/qc/plots/coverage_map.png")
plot_channel_map(session, 4096, f"{session}/qc/plots/channel_map_center.png")
plot_representative_spectrum(
    "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411", 5,
    f"{session}/qc/plots/representative_spectrum_point5.png",
)
```

**Reality check (section 101), stated plainly**: the real point-5
spectrum shows a strong, very narrow spike near +150 km/s LSRK. A real
~20 km/s-wide thermal HI line would not look like that - this is far
more consistent with narrowband RFI than a genuine detection. The
default +-100,000 m/s integration window used in `run` above integrates
mostly noise and residual baseline over a very wide band; the resulting
integrated map's structure (a smooth gradient across the field) is not
claimed here as an astrophysical signal - it is exactly the kind of
coverage/weight-edge artifact this pass's own synthetic tests
(`test_constant_sky_recovers_approximately_constant_map`,
`test_noise_only_map_averages_near_zero`) were built to catch and
distinguish from a real source. No source claim is made in this
runbook, per `docs/SCIENCE_SCOPE.md`'s own rule.

## 6. Replay determinism, without a dedicated CLI command

No `almita_science.py replay` exists in V1 (see `docs/SCIENCE_SCOPE.md`).
To confirm determinism directly:

```
./.venv/bin/python almita_science.py run <same reduce_session_dir> --beam-fwhm-deg 1.5 --output-root /tmp/science_check_a
./.venv/bin/python almita_science.py run <same reduce_session_dir> --beam-fwhm-deg 1.5 --output-root /tmp/science_check_b
```

then compare each `cube/science_cube.h5`'s arrays with `h5py` + `numpy.
array_equal(..., equal_nan=True)` - verified byte-for-byte identical in
this pass's own test suite
(`test_science_storage_and_security.py::test_two_runs_of_the_same_input_and_config_are_numerically_identical`).

## 5. Replay and compare

```
./.venv/bin/python almita_science.py replay <SCIENCE_SESSION_DIR>      # same REDUCE input + config -> NEW session, then compare
./.venv/bin/python almita_science.py compare <SESSION_A> <SESSION_B>   # read-only
```
Replay refuses if the REDUCE manifest changed (`--allow-changed-input` overrides) and never touches the original.
Per-artifact classes: BYTE IDENTICAL, NUMERICALLY IDENTICAL, NUMERICALLY EQUIVALENT, EXPECTED DIFFERENCE (input/config
differs), UNEXPECTED DIFFERENCE (a determinism bug). Exit 0 = equivalent, 1 = different.
