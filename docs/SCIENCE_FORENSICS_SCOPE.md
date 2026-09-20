# SCIENCE FEATURE FORENSICS V1: scope

Question: *does an observed spectral structure follow the sky, or capture time / point order / the receiver / RFI?*
SCIENCE V1 ([SCOPE](SCIENCE_SCOPE.md), [FREEZE](SCIENCE_V1_FREEZE.md)) answers "what structure do the Level 1 spectra produce"; forensics
answers "does that structure belong to the sky". It is **additive**: `science_forensics/` and `almita_science_forensics.py` consume
the frozen products and change nothing in `science_engine/`, `reduce_engine/`, OBSERVE, ALIGN or CALIBRATE.

## What it does
- Takes an **operator-specified window** (frame + centre + half-width) and measures the feature in every Level 1 point spectrum:
  signed peak, centroid (three estimators), width, area, mask overlap and formal uncertainty.
- Expresses the same centroids in **three frames** - LSRK velocity, topocentric frequency, channel - and reports the scatter of each.
- Regresses the centroid on **time**, **point order** and **sky position** (separately and together) and states explicitly when the
  scan pattern makes those confounded.
- Builds diagnostic tables and figures: feature track (CSV), centroid vs time / point index, sky-position maps, Level 1
  point-order x spectral-axis waterfalls (LSRK and topocentric frequency; sorted by capture, RA, Dec, Galactic l).
- Audits the **negative integrated maps**: windows (candidate + control), equal-velocity intervals, cumulative integral, median
  spectrum, empirical-vs-reported scatter, and (read-only) the distribution of an existing SCIENCE integrated map.

## What it deliberately does not do
- It never cleans, subtracts, corrects or re-baselines anything, never trains against a reference and never optimises a window.
- It never names the nature of a feature. Candidate status is `UNCLASSIFIED` (or `INSUFFICIENT_EVIDENCE`); every finding is worded
  OBSERVED / MEASURED / CONSISTENT WITH / INCONSISTENT WITH / UNRESOLVED.
- No RAW, no `data/mosaic`, no hardware, no network, no catalog or HI4PI access.
- No blind line finder, no PCA/ML, no temperature or gain correction, no cross-campaign stacking (campaigns are compared, never merged).

## Inputs and outputs
Input: one REDUCE session directory (Level 1); optionally one SCIENCE session directory, read-only. Nothing is ever written inside
either. Output root `data/science_forensics/CAMPAIGN/FORENSICS-<utc>/` (immutable; a collision is an error).

## Not available in Level 1 (reported, never inferred)
SDR/LNA temperature and receiver gain (RAW-only metadata), per-point RFI_REF association (known REDUCE V1 gap). Only the receiver
label carried by `capture_refs` is persisted.

See [MODEL](SCIENCE_FORENSICS_MODEL.md) and the [RUNBOOK](SCIENCE_FORENSICS_RUNBOOK.md).
