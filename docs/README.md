# ALMITA documentation index

## Pipeline documents (current)
| System | Documents |
|---|---|
| ALIGN | [HI_ALIGNMENT_MODEL](HI_ALIGNMENT_MODEL.md), [HI_FIRST_LIGHT_RUNBOOK](HI_FIRST_LIGHT_RUNBOOK.md) |
| CALIBRATE | [CALIBRATION_SCOPE](CALIBRATION_SCOPE.md), [CALIBRATION_MODEL](CALIBRATION_MODEL.md), [CALIBRATION_FIRST_TEST_RUNBOOK](CALIBRATION_FIRST_TEST_RUNBOOK.md) |
| REDUCE V1 (frozen) | [SCOPE](REDUCE_SCOPE.md), [MODEL](REDUCE_MODEL.md), [PIPELINE](REDUCE_PIPELINE.md), [RFI_REF interface](REDUCE_RFI_REF_INTERFACE.md), [ACCEPTANCE](REDUCE_ACCEPTANCE.md), [FREEZE](REDUCE_V1_FREEZE.md), [RUNBOOK](REDUCE_FIRST_RUNBOOK.md) |
| SCIENCE V1 (frozen) | [SCOPE](SCIENCE_SCOPE.md), [MODEL](SCIENCE_MODEL.md), [PIPELINE](SCIENCE_PIPELINE.md), [ACCEPTANCE](SCIENCE_ACCEPTANCE.md), [FREEZE](SCIENCE_V1_FREEZE.md), [RUNBOOK](SCIENCE_FIRST_RUNBOOK.md) |
| Web console | [WEB_ALIGNMENT_CALIBRATION](WEB_ALIGNMENT_CALIBRATION.md) |
| SCIENCE feature forensics (additive diagnostics) | [SCOPE](SCIENCE_FORENSICS_SCOPE.md), [MODEL](SCIENCE_FORENSICS_MODEL.md), [RUNBOOK](SCIENCE_FORENSICS_RUNBOOK.md) |
| SCIENCE forensics experiment design (field plan that breaks time/sky confounding) | [EXPERIMENT RUNBOOK](SCIENCE_FORENSICS_EXPERIMENT_RUNBOOK.md), plan `examples/science_forensics_experiment.yaml` |
| SCIENCE forensics field execution pack (operator commands: status/precheck/preflight/run/postcheck/analyze) | [FIELD RUNBOOK](SCIENCE_FORENSICS_FIELD_RUNBOOK.md), `scripts/science_forensics_field.py` |
| SCIENCE forensics indoor bench (infrastructure checks with zero mount movement; not astronomy) | [INDOOR BENCH](SCIENCE_FORENSICS_INDOOR_BENCH.md), `scripts/science_forensics_bench.py` |
| Web (Field Console :8088 + Observe API :8090): architecture, operations, test report | [ARCHITECTURE](WEB_ARCHITECTURE.md), [OPERATIONS](WEB_OPERATIONS.md), [TEST REPORT](WEB_TEST_REPORT.md) |

## Project overview
[ALMITA_OVERVIEW](ALMITA_OVERVIEW.md) - the long-form project guide (hardware, observation workflow, grid generator, alignment,
antenna and gain characterisation, RFI reference receiver). It was the repository README before the root reorganisation.

## Observation planning - `observe/`
[GRID_GENERATOR_USAGE](observe/GRID_GENERATOR_USAGE.md), [GRID_SCAN_GUIDE](observe/GRID_SCAN_GUIDE.md),
[GRID_DISTRIBUTION_CHANGES](observe/GRID_DISTRIBUTION_CHANGES.md), [README_GRID_GENERATOR](observe/README_GRID_GENERATOR.md)

## Hardware - `hardware/`
[MOUNT_SLEW_MODEL](hardware/MOUNT_SLEW_MODEL.md), [RTL_TCP_SYSTEMD](hardware/RTL_TCP_SYSTEMD.md)

## History - `history/`
Early-era and one-off documents kept for the record (Spanish setup guides, the first workflow description, the 50-point RFI_REF
end-to-end summary, an old GitHub README). They describe files by the paths they had at the time: the scripts they mention
(`analyze_spectra.py`, `plot_sky_map.py`, ...) now live under [`scripts/`](../scripts/README.md), and the installation helpers they
mention are in `scripts/legacy/`. Some diagrams they reference were never committed.

## Public project page
[`site/`](site/) is the source of the public GitHub Pages landing page (static HTML, `robots.txt`, `sitemap.xml`). It is the only
directory the Pages workflow publishes; everything else under `docs/` is not deployed.

## Note on test names
Several documents (notably the REDUCE and SCIENCE acceptance/freeze documents) refer to test files by bare name, for example
`test_reduce_models.py`. Those files are in [`tests/`](../tests/); the documents are kept unedited as evidence.
