# SCIENCE FEATURE FORENSICS V1: model

Schema `1.0`, pipeline `science-forensics-v1.0`. Everything is computed from REDUCE Level 1 point spectra (the least transformed valid
product: gridding would mix neighbouring pointings). Numbers quoted come from the synthetic acceptance and the real runs in the
[RUNBOOK](SCIENCE_FORENSICS_RUNBOOK.md).

## Frames and the LSRK relation
Level 1 stores, per point, the topocentric frequency `f` (one axis shared by the whole campaign) and the LSRK velocity. Verified on real
data (deviation 0.0000 m/s across channels) and enforced at ingest:

    v_lsrk(channel) = c * (1 - f(channel) / f_rest) + shift_point          f_rest from the REDUCE session config.json

`shift_point` (the per-point LSRK correction, real range ~1.1 km/s over the 9-point campaign) is **derived** from the stored axes, not
recomputed from ephemerides. A feature stationary in the sky has constant LSRK velocity and a topocentric frequency that follows
`-shift_point`; a receiver-fixed feature is the reverse. A feature that moves in both frames is stationary in neither.

## Window and measurement
Window: `window_frame` in `lsrk_velocity` (m/s, each point's own axis), `frequency` (Hz, topocentric) or `channel`; centre and half-width
> 0 explicit. Per point, inside the window, usable bins = mask GOOD and finite value and finite sigma > 0 (masked bins are missing,
never zero). The feature is **signed**. Three centroids are always stored: parabolic peak, half-maximum weighted centroid and a
Gaussian + constant local offset fitted by Levenberg-Marquardt (pure numpy); the primary falls back gaussian -> halfmax -> peak and the
fallback is flagged. The constant offset belongs to the window only; the spectrum is never re-baselined (test-enforced).

    y(x) = A exp(-(x-mu)^2 / (2 s^2)) + o          FWHM = 2.3548 s          area = A s sqrt(2 pi) * channel_width

Formal centroid sigma from the fit covariance (Monte Carlo of 300 realisations: centroid z-scores have mean ~0, std 0.85-1.15).
Detection gate: `|A| / median(sigma_formal) >= min_snr_formal` (default 5); below it the point is `BELOW_DETECTION_THRESHOLD`, its
numbers stay in the track for transparency, and it does not enter campaign statistics. Other statuses: `NO_USABLE_BINS`,
`INSUFFICIENT_BINS`, `WINDOW_OUTSIDE_SPECTRUM`, `NO_FEATURE_SIGNAL`. Flags: `WINDOW_TRUNCATED`, `EDGE_OF_WINDOW`, `MASK_OVERLAP`
(> 30 % of window bins masked), `BROAD_VS_WINDOW` (FWHM > 50 % of the window), `GAUSSIAN_FIT_REJECTED`, `FALLBACK_TO_*`.
Level 1 sigma is statistical only (REDUCE documents ~2x between-point scatter in normal bins), so formal significance is not overstated:
`empirical_noise` reports the across-point scatter of feature-free control channels against the reported sigma.

## Campaign statistics
- **Frame coherence table** (core output): for LSRK velocity, topocentric frequency and channel bin: RMS, robust RMS, range, RMS in channel
  widths, and RMS after a linear time fit; plus the RMS expected for centroids drawn uniformly at random inside the window
  (`window_bins / sqrt(12)`), to tell a localised feature from noise peaks.
- **Models** (OLS, numpy): centroid ~ null, ~ time, ~ point order, ~ RA+Dec offsets, ~ time+RA+Dec, for LSRK and frequency; R2, adjusted
  R2, residual RMS, AIC/AICc, coefficient standard errors, partial R2.
- **Confounding**: correlation matrix of time / point index / RA offset / Dec offset, VIF, standardised condition number.
  `confounded_time_sky` is true when |corr(time, sky offset)| >= 0.7 or any VIF >= 5. Serpentine mosaics entangle time with Dec.
- **Drift**: linear slope vs time (Hz/hour, channels/hour, m/s/hour with SE and z) and vs point order (per point).
- **Evolution**: peak, FWHM, area, formal SNR, mask fraction vs time with slope, z and correlations with time, RA, Dec.
- **Separate metrics, no composite score**: LSRK_stability, frequency_stability, spatial_coherence, time_drift, mask_overlap, formal SNR.
- **Evidence statements**: rule-based, each with its label, basis and numbers. Frame rule: with a noise floor of `max(3*formal sigma,
  0.5 ch)`, a frame ratio >= 3 gives CONSISTENT WITH stationarity in the steadier frame; both above the floor and comparable gives
  INCONSISTENT WITH a stationary feature in either frame; both below gives UNRESOLVED. Time/sky attribution is only offered when not
  confounded, otherwise UNRESOLVED. Regressions are diagnostics, **not causal inference**.

## Negative-map audit
Per-point Level 1 integral of relative intensity over an LSRK window (midpoint rule with local channel width, masked bins skipped;
relative_intensity*m/s), for: the candidate window; left/right **control windows** of the same width separated by a guard gap of
`control_guard_fraction` x width, chosen for spectral validity (inside every point's coverage, valid fraction >= 0.8 in >= 80 % of
points, never for their sign; all attempts persisted); the integration window (default the SCIENCE V1 window +/-100 km/s); the
integration window excluding the candidate; `n_velocity_intervals` equal intervals (median, p16-p84, fraction negative, coverage);
cumulative integral along velocity (campaign median/p16/p84 + representative points); median spectrum across points with robust spread;
by REDUCE quality state. If a SCIENCE session is given (must derive from the same REDUCE session), its integrated map is described
read-only (distribution, terciles of pointing count). Nothing here changes a canonical product.

## Sessions and provenance
Files: `manifest.json`, `config.json`, `provenance.json`, `feature_track.csv`, `feature_summary.json`, `frame_coherence.json`,
`models.json`, `negative_map_audit.json`, `median_spectrum.csv`, `cumulative_integral.csv`, PNG figures, `summary.md`. The manifest
lists every product with sha256 and a `numeric_sha256` over the seven numeric files: same Level 1 + same config gives an identical
`numeric_sha256` (verified on real data). Provenance records the REDUCE manifest sha256, per-point Level 1 hashes and capture ids,
the SCIENCE manifest sha256 if used, git commit, config hash, `raw_access: none`, `network_access: none`. Sidereal time (optional,
`--site-from-observer-config`, offline, UT1-UTC set to 0) is reported per point; over a short campaign it is collinear with time.
`validate` checks files, schema, hashes, finite JSON, track rows and candidate status; `compare` reports EQUIVALENT / EXPECTED
DIFFERENCE / UNEXPECTED DIFFERENCE.

## Limitations
Operator-chosen window; a very small campaign has few degrees of freedom; scan patterns confound time and sky; Gaussian model is an
approximation (real reduced chi-square 2-18); temperature, gain and RFI_REF association unavailable; no absolute calibration
anywhere; diagnostics are not proof of causality or of nature.
