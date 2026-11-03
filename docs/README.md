# ALMITA documentation index

**Current state (November 2026):** see [What is new](../README.md#what-is-new-up-to-november-2026) in the root README —
notebook + Raspberry Pi 4 capture box, the full web console (STATUS … TRANSIT … SESSIONS), live RFI masks and the DIY feed v1.
The newest code and its web documents live on the development branch
[`rfi-masks-v1`](https://github.com/almita-radio/almita/tree/rfi-masks-v1) until they are merged here:
[WEB_ARCHITECTURE](https://github.com/almita-radio/almita/blob/rfi-masks-v1/docs/WEB_ARCHITECTURE.md),
[WEB_OPERATIONS](https://github.com/almita-radio/almita/blob/rfi-masks-v1/docs/WEB_OPERATIONS.md),
[SESSIONS_FILE_CONTRACTS](https://github.com/almita-radio/almita/blob/rfi-masks-v1/docs/SESSIONS_FILE_CONTRACTS.md),
[BACKLOG](https://github.com/almita-radio/almita/blob/rfi-masks-v1/docs/BACKLOG.md).

## Pipeline documents (current)
| System | Documents |
|---|---|
| ALIGN | [HI_ALIGNMENT_MODEL](HI_ALIGNMENT_MODEL.md), [HI_FIRST_LIGHT_RUNBOOK](HI_FIRST_LIGHT_RUNBOOK.md) |
| CALIBRATE | [CALIBRATION_SCOPE](CALIBRATION_SCOPE.md), [CALIBRATION_MODEL](CALIBRATION_MODEL.md), [CALIBRATION_FIRST_TEST_RUNBOOK](CALIBRATION_FIRST_TEST_RUNBOOK.md) |
| REDUCE V1 (frozen) | [SCOPE](REDUCE_SCOPE.md), [MODEL](REDUCE_MODEL.md), [PIPELINE](REDUCE_PIPELINE.md), [RFI_REF interface](REDUCE_RFI_REF_INTERFACE.md), [ACCEPTANCE](REDUCE_ACCEPTANCE.md), [FREEZE](REDUCE_V1_FREEZE.md), [RUNBOOK](REDUCE_FIRST_RUNBOOK.md) |
| SCIENCE V1 (frozen) | [SCOPE](SCIENCE_SCOPE.md), [MODEL](SCIENCE_MODEL.md), [PIPELINE](SCIENCE_PIPELINE.md), [ACCEPTANCE](SCIENCE_ACCEPTANCE.md), [FREEZE](SCIENCE_V1_FREEZE.md), [RUNBOOK](SCIENCE_FIRST_RUNBOOK.md) |
| Web console | [WEB_CONSOLE_SCREENSHOTS](WEB_CONSOLE_SCREENSHOTS.md) (one screen per page), [WEB_ALIGNMENT_CALIBRATION](WEB_ALIGNMENT_CALIBRATION.md) |

## Project overview
[ALMITA_OVERVIEW](ALMITA_OVERVIEW.md) - the long-form project guide (hardware, observation workflow, grid generator, alignment,
antenna and gain characterisation, RFI reference receiver). It was the repository README before the root reorganisation.

## Observation planning - `observe/`
[GRID_GENERATOR_USAGE](observe/GRID_GENERATOR_USAGE.md), [GRID_SCAN_GUIDE](observe/GRID_SCAN_GUIDE.md),
[GRID_DISTRIBUTION_CHANGES](observe/GRID_DISTRIBUTION_CHANGES.md), [README_GRID_GENERATOR](observe/README_GRID_GENERATOR.md)

## Hardware - `hardware/`
[README](hardware/README.md) (current architecture), [BUILD](hardware/BUILD.md), [BOM](hardware/BOM.md),
[RF_AND_WIRING](hardware/RF_AND_WIRING.md) (including the computers as RF sources), [MECHANICAL](hardware/MECHANICAL.md) (DIY feed v1),
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
