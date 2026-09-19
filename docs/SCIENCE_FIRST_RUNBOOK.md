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

`--beam-fwhm-deg 1.5` matches THIS campaign's own known
`grid_metadata.json` spacing (learned out-of-band, not auto-discovered
by SCIENCE - see `docs/SCIENCE_SCOPE.md`'s beam-gap section). Omitting
it falls back to `observer_config.json`'s site-wide default (20.0),
which is far larger than this field and would blend every point into
one beam - fine for a quick check, misleading for a real map.

Expected: grid shape, beam summary, memory/disk estimate, all checks OK.

## 3. Run

```
./.venv/bin/python almita_science.py run \
  "data/reduced/ALMITA-WEB-SMALL-RUN-01/REDUCE-20260919-225900-910411" \
  --beam-fwhm-deg 1.5 --output-root data/science
```

Real measured result (this pass): `SCIENCE COMPLETED`, 21x21 grid, 8192
velocity channels, quality `WARNING` (reason:
`BEAM_MODEL_PROVISIONAL` - always present in V1, honest by design),
~2.8-3.0s wall time, 115 MB output. See `docs/SCIENCE_PIPELINE.md`'s
Performance section.

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
