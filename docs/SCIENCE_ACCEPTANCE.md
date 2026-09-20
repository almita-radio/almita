# SCIENCE V1 - Acceptance evidence (second pass)

Question of this pass: *can we trust the LEVEL 2 products scientifically?* Everything below was measured on this Pi
(single driver, runs executed sequentially). Foundation commit `fd9ca3a`; REDUCE (`2afc4c5`) untouched (`git diff` on
`reduce_engine/` and `almita_reduce.py` is empty). Reproduce with `pytest tests/test_science_*.py -s` (tables print) and
`scripts/validation/science_acceptance_measure.py`.

## 1. Test counts
`pytest tests/test_science_*.py` -> **296 passed** in ~36 s (foundation had 75; the first stopped attempt of this pass left 41).
Per file: beam 10, beam_contract 18, boundaries 57, cube 11, golden 11, gridding 20, gridding_truth 53, ingest 6,
integration 5, integration_truth 15, moments 4, nan_matrix 21, resample_strict 11, resample_truth 10, self_description 3,
sigma_audit 13, spatial 9, storage_and_security 10, uncertainty_and_coverage 4, uncertainty_mc 5.
REDUCE smoke subset (`contract_and_integrity`, `models`, `mask_contract`): 30 passed (REDUCE not modified; not the full REDUCE suite).

## 2. Bugs and gaps found (each demonstrated by a failing check before the fix)
| # | finding | fix |
|---|---|---|
| 1 | resample emitted GOOD bins whose value was NaN (0.999 tolerance) | explicit bracket/weight resampler, no tolerance |
| 2 | a NaN/Inf value flagged valid poisoned the voxel (the old test enshrined it as "fails loud") | validity enforced inside `add_point` |
| 3 | `1/sigma^2` overflow after 4 contributors at sigma ~1.5e-154; `weight_sum**2` overflow gave sigma == 0 | floor derived from float64 range; two divisions |
| 4 | beam default 20 deg claimed to come from `observer_config.json` but was never read | honest placeholder; CLI requires an explicit beam; path/field/sha256 recorded |
| 5 | `run` had no preflight (real 4.7 GB OOM-kill during this pass, another session) | preflight before allocation and before a session exists |
| 6 | window partly outside the cube silently clipped; median dv; no narrow-window semantics | bin-overlap integration, `window_status`, spectral coverage |
| 7 | provisional beam forced every product to WARNING | separate `limitations` |
| 8 | ingest: NaN coordinates accepted, silent skips, no hashes/duplicates/frame check, order-dependent | exclusions recorded, sha256, capture-duplicate detection, sort by index |
| 9 | canonical velocity axis depended on the first point and was descending | ascending, lowest-index reference |
| 10 | no FAILED/CANCELLED state, non-atomic HDF5, trivial validator | lifecycle states, tmp+rename, strict validator (13 corruption cases) |
| 11 | `moment1_like` let window noise in: centroid bias 1.3 channel at a high-SNR pixel | per-channel signal gating (bias 0.07 channel) |
| 12 | uncertainty/coverage/channel PNGs printed "beam FWHM=?"; "coverage" plot was n_pointings | read beam from the session; renamed |
| 13 | `session_id=""` silently replaced by a generated id | rejected |
| 14 | REAL data: sigma false precision (see 7) | local sigma consistency check |
Also: `n_contributing` renamed `n_pointings` in cube/maps (REDUCE's `n_contributing` is a different quantity).

## 3. Resample truth (analytic Gaussian; shift in channels; dv = 61.83 m/s)
Broad line, FWHM 100 ch (real HI scale): for shifts 0.1 to 83.7 ch (incl. the real 10.4, 17.4 and 83.7): centroid error 0.000 m/s,
FWHM error 0 to +0.0139 %, area error < 1e-6 %, peak loss <= 1.4e-4.
Narrow line, FWHM 4 ch (worst case): FWHM error 0 / 2.0 / 4.7 / 8.3 / 0 / 6.5 % for shifts 0 / 0.1 / 0.25 / 0.5 / 1.0 / 10.37 ch,
peak loss up to 0.0796 (8 %), area error 0.000 %, centroid error 0. Descending source axis gives identical results.
Two components (5 and 30 km/s wide) with an RFI notch: positions within 18 m/s, amplitudes 0.9999 / 0.6000, the notch is never
filled (edge dilation: 11 bins). Interpolation is value-preserving in area, **not** peak-preserving for lines of a few channels.
Sigma convention (Monte Carlo, empirical/reported): per-channel 1.000 / 0.791 / 0.707 and integrated 0.993 / 1.027 / 0.983 for
shifts 0 / 0.25 / 0.5 ch.

## 4. Gridding and uncertainty truth
- Independent manual evaluation (own haversine, plain floats): rtol 1e-12. Constant sky: max|bias| 4.4e-16 (RMS 7.7e-17) with 4-25
  overlapping pointings; zero sky exactly 0 where covered, NaN elsewhere; two identical points: value 1.0 (not 2.0), sigma/sqrt(2).
- Noise-only (400 realisations): z mean +0.107, std 0.979, corr(z, coverage) -0.055.
- Monte Carlo, 600 realisations, no resampling: empirical/reported 0.999 per channel, 1.040 integrated; truth within 1 sigma 0.685
  (0.683), within 2 sigma 0.954 (0.954). With LSRK offsets (resampled): 0.940 per channel, 0.959 integrated, 0.715, 0.968.
- Adjacent-pixel noise correlation r = 0.744 (6 pixels away 0.418): per-pixel sigma does not imply independent pixels.
- Golden fixture (spatial x spectral Gaussian, velocity gradient, noise, notch, LSRK offsets, descending axes, one BAD, one missing
  point) through the production path vs an independent forward model of the whole cube: 402,581 voxels, z mean -0.015, rms 0.837,
  max|z| 3.66. Spatial FWHM 1.85 / 1.85 deg (predicted 1.92); centre matches the model to < 0.1 deg; integrated-map z rms 1.15;
  sigma at the edge 1.98x the centre; velocity gradient 4730 m/s/deg (independent beam-smoothed model 4493; unsmoothed truth 8000:
  **gradients are beam-smoothed**); centroid at the source pixel within 56 m/s of the model; BAD point (value 50) excluded; the
  missing pointing lowers `n_pointings`/weight but does not blank the pixel.
- Unmasked extreme spike (30) in one pointing: confined to that pointing's beam support (|excess| < 0.2 beyond cutoff); masked: vanishes.
- False-good / false-bad: 8 adversarial scenarios (all BAD, all masked, no beam support, sigma 50, sigma 1e300, all-NaN
  coordinates, window outside cube, no velocity) -> 0 false-good; 3 healthy fixtures -> 0 false-bad.
- Integration: bin-overlap error on an analytic Gaussian <= 1e-13 % for FWHM 100/20/8/4 ch; exact on irregular axes, narrow
  windows and windows off channel boundaries; masked channels lower value and coverage, never interpolated.

## 5. Beam cutoff sensitivity (real 9-pt, FWHM 1.5 deg)
| cutoff | valid voxels | valid pixels | flux proxy (sum of integrated map) vs 3x | median sigma_I | runtime |
|---|---|---|---|---|---|
| 1x | 0.572 | 263 | -41.8 % | 118.6 | 2.19 s |
| 1.5x | 0.881 | 405 | -8.6 % | 118.1 | 2.15 s |
| 2x | 0.956 | 439 | -0.29 % | 116.6 | 2.28 s |
| 3x | 0.962 | 441 | 0 | 116.8 | 2.12 s |
Default 3x kept (no evidence to change).

## 6. Real campaigns (SCIENCE reads only Level 1; beams passed by the operator from each campaign's grid, NOT measured)
| | 9-pt COMPLETED | 100-pt COMPLETED | PARTIAL (50 of 100 COMPLETED) |
|---|---|---|---|
| REDUCE session | `REDUCE-20260919-225900-910411` | `REDUCE-20260919-234712-712870` | `REDUCE-20260919-235213-947275` |
| campaign | ALMITA-WEB-SMALL-RUN-01 | ALMITA-OBSERVE (2026-09-14) | ALMITA-OBSERVE (2026-09-02) |
| quality of inputs | 9 GOOD | 100 GOOD | 50 GOOD (+50 BLOCKED) |
| extent | RA 175.6-179.3, Dec -34.9..-31.9 | RA 343.5-356.2, Dec -38.4..-28.3 | RA 217-309, Dec -74.9..-58.3 |
| beam (operator) | 1.5 deg | 1.1111 deg | 3.3333 deg |
| grid, cube | 21x21, 8192x21x21 | 49x51, 8192x49x51 | 33x56, 8192x33x56 |
| velocity range | -272.9..+233.6 km/s | -265.4..+241.1 | -277.4..+229.1 |
| offset vs reference (max / pairwise spread) | 10.4 / 17.4 ch | 83.7 / 83.7 ch | 72.6 / 77.0 ch |
| fraction interpolated / edge bins lost (max) | 0.889 / 11 | 0.990 / 84 | 0.980 / 73 |
| masked input -> valid after resample | 0.0500 -> 0.9426 | 0.0500 -> 0.9417 | 0.0500 -> 0.9418 |
| SCIENCE status / completeness / quality | COMPLETED / COMPLETE / GOOD | COMPLETED / COMPLETE / GOOD | COMPLETED / PARTIAL / WARNING |
| limitations | PROVISIONAL_BEAM_MODEL, VELOCITY_RESAMPLED | same | + INPUT_PARTIAL |
| runtime / peak RSS / output | 2.3 s / 282 MB / 119 MB | 77.6 s / 1237 MB / 676 MB | 31.2 s / 928 MB / 500 MB |
| valid voxel / valid map fraction | 0.962 / 1.000 | 0.964 / 1.000 | 0.927 / 0.962 |
| median cube sigma | 0.0303 | 0.0247 | 0.0268 |
| median integrated sigma_I | 119.4 | 97.6 | 107.7 |
| Spearman(weight_sum, 1/sigma_I) | +0.677 | +0.680 | +0.661 |
Validator OK on all three. All three finite/non-zero maps, no hot pixels (> 8 MAD). Plan estimates 296 / 1213 / 923 MB were
within budget (measured 282 / 1237 / 928 before the input-memory term was added; the estimator now includes it).
Velocity-grid offsets are real LSRK geometry, larger than the 641 m/s / 10.4 channels quoted after the first pass (that was
relative to point 0; the 100-point spread is 5.2 km/s).
**Determinism:** `replay` of the 9-, 50- and 100-point sessions -> BYTE IDENTICAL cube and three maps; shuffled listing order
gives identical arrays (`arrays_sha256`).

Real-data observations (no cause claimed):
- Integrated maps are strongly negative (9-pt: median -1575 relative*m/s vs sigma_I 119; PARTIAL median -3531): the
  mean relative intensity over +-100 km/s is offset by several sigma, which comes from REDUCE's baseline.
- The narrow feature near +148..153 km/s seen in the 9-pt spectra is unmasked in REDUCE. Its peak channel drifts monotonically
  with capture order (channel 1376 -> 1303, 21 kHz; LSRK 148.5 -> 153.2 km/s across points). It is therefore inconsistent with a
  fixed-velocity sky line and with a fixed-frequency carrier; it behaves as a drifting narrowband signal and **warrants RFI /
  instrument investigation**. SCIENCE preserves it per pointing (no smearing beyond the LSRK offset spread: its width in the
  cube is ~47 channels vs 16-23 in single points); it is not hard-coded as RFI anywhere.
- Visual review: no all-NaN/all-zero, no edge fill, no RA flip or velocity inversion. The lattice in sigma/pointing-count maps is
  the point sampling (beam FWHM ~ point spacing) and the hard cutoff ripple of `n_pointings`; the cross-hatched channel maps are
  point-to-point differences in the data.

## 7. Sigma audit of REDUCE Level 1 (100 real points)
REDUCE's uncertainty is one fixed per-bin profile identical in every point and campaign (median 0.0399). 63 bins per point are
below 0.1 x the global median (67 by the local criterion), isolated single channels; the empirical across-point scatter there is
0.085 (equal to normal bins, 0.084) against a reported median sigma of 3.2e-3: sigma is 20-850x (median 27.6x) too small. Across
normal bins the between-point scatter is 2.1x the reported sigma (median; p5 0.9, p95 7.7). Consequences: (a) the isolated dips are
excluded by the local check (min cube sigma / median 0.004 -> 0.127); (b) SCIENCE sigma is statistical only and a lower bound.

## 8. Performance, memory, output
Stage breakdown (100-pt real, 77.6 s): cube accumulation 70.6 s (91 %), persistence 3.0 s, integrated products 1.4 s, QC 0.4 s,
ingest 0.3 s. Top-3 bottlenecks: accumulation, persistence, integrated products. Not optimised (not pathological).
Memory model, measured: 57.5 B/voxel (3.6 M voxels), 57.3 (7.4 M), 57.1 (20.5 M). Stress 50x50x8192 (20.48 M voxels): estimate 1213 MB,
peak 1194 MB; file 644.6 MB (estimate 645.5); cube 9.5 s, maps 1.4 s, write 0.66 s. Which arrays dominate (21x21x8192): four 27.6 MB
float64/int64 arrays and a 3.4 MB bool.
float32 study (real cube): intensity max abs error 3.0e-8 (3.7e-8 of peak), sigma 6e-8 relative, velocity axis 0.016 m/s (0.00025
channel), integrated map 2.1e-6 of peak, its sigma 4e-7 relative: harmless but not adopted (2x disk only).
HDF5 layout (21x21x8192): contiguous none 113.7 MB, write 0.12 s, channel 0.07 ms, spectrum 5.4 ms, cube 29 ms; lzf 81.5 MB (write 2.6 s);
gzip-1 76.2 MB (write 3.2 s, cube read 210 ms); (64,ny,nx) chunks make spectrum reads 26-180 ms. Unchanged.

## 9. Known limitations (V1)
1. Beam is operator-provided operational metadata (provisional); no measured beam exists.
2. No absolute calibration; relative units only.
3. Uncertainty = propagated statistical sigma only (a lower bound; real between-point scatter is ~2.1x); no spatial covariance;
   neighbouring pixels are beam-correlated (r = 0.74).
4. Narrow features of a few channels are smoothed by resampling (peak loss up to 8 %, area conserved); masked bins can widen by one bin.
5. Recovered velocity gradients and spatial structure are beam-smoothed.
6. Local tangent-plane-like grid: pixel footprint varies with declination; consistent but approximate for wide fields (the PARTIAL
   session spans ~92 deg x 17 deg).
7. No spatial outlier rejection: an extreme GOOD point contributes per the formula.
8. Hard beam cutoff gives an integer ripple in `n_pointings`; use `weight_sum` for smooth support.
9. Effective integration time is not produced.
10. Moment-like maps are experimental-grade (gated, but not a professional moment reduction).
11. Accumulation cost grows with points x voxels (78 s for 100 points x 20 M voxels).
12. Not evaluated in this pass: l-v diagram, FITS, HI4PI comparison, cross-campaign stacking (all deferred by instruction); float32 /
    compression / chunking (measured, not adopted); thread nondeterminism only through repeated runs; the tail of the requested
    checklist beyond section 137 was truncated in the request and was not seen.
