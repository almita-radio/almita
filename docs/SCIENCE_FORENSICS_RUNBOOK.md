# SCIENCE FEATURE FORENSICS V1: runbook

Run from the repository root with the project virtualenv. Everything is offline and read-only with respect to REDUCE/SCIENCE sessions.

## 1. Locate a candidate window from the data (read-only)
```
./.venv/bin/python almita_science_forensics.py inspect data/reduced/<CAMPAIGN>/<REDUCE_SESSION> --scan-v-min 130000 --scan-v-max 170000
```
Prints the campaign summary and, per point, the strongest smoothed excursion in the scanned LSRK range, plus a *suggested* window
(operator decides).

## 2. Plan (no writes) and run
```
./.venv/bin/python almita_science_forensics.py plan <REDUCE_SESSION> --center 150857 --half-width 5607 \
    [--window-frame lsrk_velocity|frequency|channel] [--science-session <SCIENCE_SESSION>] [--site-from-observer-config]
./.venv/bin/python almita_science_forensics.py run  <REDUCE_SESSION> --center 150857 --half-width 5607 --feature-id MYCAND \
    --science-session <SCIENCE_SESSION> --site-from-observer-config --output-root data/science_forensics
./.venv/bin/python almita_science_forensics.py validate <FORENSICS_SESSION>
./.venv/bin/python almita_science_forensics.py compare <SESSION_A> <SESSION_B>
```
`plan` lists blocked reasons (window outside the spectrum, too few points, SCIENCE session from another REDUCE session); `run`
refuses to start if blocked. Read `summary.md` first, then `frame_coherence.json`, the two `centroid_vs_*.png` and the waterfalls.

## 3. How to read it
- Frame table: which frame keeps the centroid steadier? Both moving = stationary in neither.
- `models.json` / confounding: if `confounded_time_sky` is true, do not attribute the trend to time or to sky.
- Detection fraction and formal SNR: a window with only noise peaks reports low SNR, ~2-channel FWHM and scatter comparable to random
  positions; it is not a persistent feature.
- Negative-map audit: compare candidate vs control windows and the "excluding candidate" integral before blaming any window.

## 4. Worked example: the real +148-153 km/s feature (campaign ALMITA-WEB-SMALL-RUN-01, 9 points, 4.3 min)
Window from the scan above: 150857 +/- 5607 m/s (LSRK). Measured (all 9 points detected, formal SNR 15.5-21, FWHM ~22 channels =
1.4 km/s = 6.6 kHz, peak +0.67, no masked bins): centroid moves from channel 1380 to 1300 (LSRK 148.26 -> 153.40 km/s, topocentric
1419.6100 -> 1419.5866 MHz = -23.4 kHz). Frame scatter (RMS): LSRK 32.5 channels, frequency 28.5 channels, channel 28.5; after a linear
time fit 9.3 / 8.4 / 8.4. Drift vs time: -3.24e5 +/- 3.5e4 Hz/hour (z = -9.3, R2 0.925), -9.9 channels per point; LSRK +7.8e4 m/s/hour.
Confounding: corr(time, Dec) = 0.95, corr(time, RA) = 0.10, VIF 11. R2 time/sky/both: LSRK 0.928/0.871/0.943, frequency
0.925/0.832/0.925. The LSRK-shift spread across points (17.4 channels) accounts for only part of the 79.7-channel motion.
Statements: INCONSISTENT WITH a feature stationary in either frame; UNRESOLVED time vs sky (confounded 3x3 raster); the width and area
decrease along the campaign (FWHM 27.4 -> 21.8 channels, area z = -3.9) but correlate equally with Dec. The same frequency window
on the 100-point and 50-point campaigns shows no comparable feature (only 55 of 100 and 10 of 50 points pass the SNR gate, median formal
SNR 6.8 and 5.7, median FWHM ~2 channels; centroid scatter 0.5-0.9 of the RMS of random positions inside the window). Nature not assigned; it requires instrument/RFI follow-up.

## 5. Negative integrated maps (same three campaigns)
Uniform broad negative offsets, not driven by the candidate window: mean relative intensity over +/-100 km/s -0.0067 (9 pt), -0.0012
(100 pt), -0.0197 (50 pt PARTIAL); excluding the candidate window the integral is unchanged in every campaign.
