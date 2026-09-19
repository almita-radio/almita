"""The canonical pipeline: INGEST -> VALIDATE -> SPECTRAL ESTIMATE -> MASK
-> RELATIVE CALIBRATION -> BASELINE -> VELOCITY FRAME -> RESAMPLE COMMON
GRID -> AVERAGE/STACK -> UNCERTAINTY -> QUALITY -> PERSIST LEVEL 1.

Each stage's output is explicit and stateless - no hidden global state
carried between points. A point that fails any stage is recorded as
BLOCKED/FAILED with a reason and the campaign continues; it never aborts
the whole run and never reports COMPLETED while silently dropping points.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from reduce_engine import averaging, baseline as baseline_mod, calibration as calibration_mod
from reduce_engine import masks as masks_mod, quality as quality_mod, resample as resample_mod
from reduce_engine import rfi_ref as rfi_ref_mod
from reduce_engine import spectral as spectral_mod, uncertainty as uncertainty_mod, velocity as velocity_mod
from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import CampaignManifest, PointRecord, metadata_field, read_capture
from reduce_engine.models import CaptureRef, MaskFlag, MasterSpectrum
from reduce_engine.provenance import build_run_provenance, close_run_provenance
from reduce_engine.storage import ReduceSession


class PointStatus:
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass
class PointOutcome:
    point_index: int
    status: str
    reason: Optional[str]
    master_spectrum: Optional[MasterSpectrum]
    timing_seconds: dict[str, float]


def reduce_point(point: PointRecord, manifest: CampaignManifest, config: ReduceConfig,
                 calibration_profile: Optional[dict[str, Any]], calibration_profile_path: Optional[str]) -> PointOutcome:
    timing: dict[str, float] = {}
    if not point.accepted:
        return PointOutcome(point.point_index, PointStatus.BLOCKED, point.reject_reason, None, timing)

    def _stage(name: str):
        return _Timer(timing, name)

    try:
        with _stage("ingest"):
            iq, attrs = read_capture(point.resolved_path)
        with _stage("spectral_estimate"):
            estimate = spectral_mod.estimate_spectrum(
                iq, sample_rate_hz=float(attrs["sample_rate_hz"]),
                center_frequency_hz=float(attrs["center_frequency_hz"]), config=config)
        with _stage("mask"):
            mask_result = masks_mod.build_mask(
                estimate.frequency_hz, estimate.psd, center_frequency_hz=float(attrs["center_frequency_hz"]),
                config=config, segments=estimate.segments, calibration_profile=calibration_profile,
                clipping_fraction=estimate.clipping_fraction)
        with _stage("calibration"):
            calibration_outcome = calibration_mod.apply_calibration(
                estimate.frequency_hz, estimate.psd, capture_path=str(point.resolved_path),
                profile=calibration_profile, profile_path=calibration_profile_path)
        with _stage("baseline"):
            baseline_result = baseline_mod.fit_baseline(estimate.frequency_hz, estimate.psd, mask_result.mask,
                                                         config=config)
        with _stage("rfi_ref"):
            # No RFI_REF-to-MAIN-capture association mechanism was found in
            # OBSERVE's real output during audit (RFI_REF captures exist
            # only in the separate CALIBRATE workflow, not per observation
            # point) - this stage still runs for real, honestly reports
            # unavailable, and is the wiring point for that association
            # once OBSERVE/RFI_REF produce one. Never blocks the pipeline.
            rfi_ref_outcome = rfi_ref_mod.evaluate_rfi_ref(
                estimate.frequency_hz, estimate.psd, baseline_result.baseline,
                ref_frequency_hz=None, ref_psd=None, ref_baseline=None, time_delta_seconds=None,
                max_time_delta_seconds=config.rfi_ref_max_time_delta_seconds,
                min_match_confidence=config.rfi_ref_min_match_confidence)
            if rfi_ref_outcome.rfi_bins.any():
                mask_result.mask[rfi_ref_outcome.rfi_bins] |= MaskFlag.RFI.value
        with _stage("velocity"):
            velocity_result = velocity_mod.compute_velocity_axis(
                estimate.frequency_hz, rest_frequency_hz=config.hi_rest_frequency_hz, frame=config.velocity_frame,
                timestamp_utc=metadata_field(attrs, "capture_start_utc"),
                observer_latitude_deg=(manifest.observer.get("observer", {}) or {}).get("latitude_deg", "UNKNOWN"),
                observer_longitude_deg=(manifest.observer.get("observer", {}) or {}).get("longitude_deg", "UNKNOWN"),
                observer_elevation_m=(manifest.observer.get("observer", {}) or {}).get("elevation_m", "UNKNOWN"),
                ra_hours=point.ra_hours if point.ra_hours is not None else "UNKNOWN",
                dec_degrees=point.dec_degrees if point.dec_degrees is not None else "UNKNOWN")

        if calibration_outcome.calibration_level == "RELATIVE":
            relative_intensity = calibration_outcome.fractional_excess
            unc_fractional = calibration_outcome.fractional_uncertainty
        else:
            relative_intensity = (estimate.psd - baseline_result.baseline) / baseline_result.baseline
            per_bin_psd_uncertainty = uncertainty_mod.per_capture_uncertainty(estimate.segments)
            unc_fractional = per_bin_psd_uncertainty / baseline_result.baseline

        with _stage("resample"):
            resampled = resample_mod.resample_to_common_grid(
                [estimate.frequency_hz], [relative_intensity], [unc_fractional], [mask_result.mask])
        with _stage("average"):
            stacked = averaging.stack_spectra(
                resampled.values, resampled.uncertainties, resampled.masks, method=config.averaging_method,
                sigma_clip_threshold=config.sigma_clip_threshold)

        integration_time_raw = attrs.get("duration_seconds", attrs.get("capture_time_seconds"))
        integration_time_known = integration_time_raw is not None
        integration_time = float(integration_time_raw) if integration_time_known else 0.0
        with _stage("quality"):
            quality_report = quality_mod.assess_quality(
                mask=stacked.mask, n_contributing=stacked.n_contributing, clipping_fraction=estimate.clipping_fraction,
                calibration_level=calibration_outcome.calibration_level,
                calibration_compatibility_status=calibration_outcome.compatibility_status,
                baseline_fit_quality_rms_fraction=baseline_result.fit_quality_rms_fraction,
                velocity_frame=velocity_result.frame, integration_time_known=integration_time_known)
            quality_report.metrics.update({
                "rfi_ref_available": rfi_ref_outcome.available, "rfi_ref_used": rfi_ref_outcome.used,
                "rfi_ref_time_delta_seconds": rfi_ref_outcome.time_delta_seconds,
                "rfi_ref_match_confidence": rfi_ref_outcome.match_confidence,
            })

        capture_ref = CaptureRef.from_capture_file(
            point.resolved_path, campaign_id=manifest.campaign_id, point_index=point.point_index,
            capture_id=str(attrs.get("final_filename", point.resolved_path.name)),
            timestamp_utc=str(metadata_field(attrs, "capture_start_utc")),
            receiver=str(metadata_field(attrs, "instrument_topology", "rf_input")))

        master = MasterSpectrum(
            campaign_id=manifest.campaign_id, point_index=point.point_index,
            ra_hours=point.ra_hours, dec_degrees=point.dec_degrees,
            timestamp_start_utc=str(metadata_field(attrs, "capture_start_utc")),
            timestamp_end_utc=str(metadata_field(attrs, "capture_completed_at")),
            frequency_hz=resampled.frequency_hz, velocity_lsrk_m_s=velocity_result.velocity_m_s,
            velocity_frame=velocity_result.frame, relative_intensity=stacked.value,
            uncertainty=stacked.uncertainty, mask=stacked.mask, n_contributing=stacked.n_contributing,
            integration_time_seconds=integration_time, quality=quality_report,
            calibration_level=calibration_outcome.calibration_level,
            calibration_profile_id=calibration_outcome.profile_id,
            calibration_profile_hash=calibration_outcome.profile_hash, capture_refs=[capture_ref])
        return PointOutcome(point.point_index, PointStatus.COMPLETED, None, master, timing)
    except Exception as error:  # noqa: BLE001 - a point failure must never abort the campaign
        return PointOutcome(point.point_index, PointStatus.FAILED, f"{type(error).__name__}: {error}", None, timing)


class _Timer:
    def __init__(self, timing: dict[str, float], name: str):
        self.timing, self.name = timing, name

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.timing[self.name] = time.monotonic() - self._start
        return False


@dataclass
class CampaignReduceReport:
    campaign_id: str
    session_id: str
    status: str  # COMPLETED | PARTIAL | FAILED
    points_discovered: int
    points_accepted: int
    points_rejected: int
    points_completed: int
    points_blocked: int
    points_failed: int
    quality_counts: dict[str, int]
    calibration_level_counts: dict[str, int]
    velocity_frame_counts: dict[str, int]
    median_usable_fraction: Optional[float]
    median_baseline_rms_fraction: Optional[float]
    runtime_seconds: float
    output_dir: str


def reduce_campaign(manifest: CampaignManifest, config: ReduceConfig, *, output_root: str,
                    calibration_profile_path: Optional[str] = None) -> CampaignReduceReport:
    from datetime import datetime, timezone
    from pathlib import Path

    from reduce_engine.models import sha256_of_file

    started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    calibration_profile = calibration_mod.load_profile(calibration_profile_path)
    calibration_profile_hash = (sha256_of_file(Path(calibration_profile_path).with_suffix(".npz"))
                                if calibration_profile_path else None)
    session = ReduceSession(output_root, manifest.campaign_id)
    session.write_config(config.to_dict())
    session.log_event("REDUCE_CAMPAIGN_BEGIN", campaign_id=manifest.campaign_id,
                      points_discovered=len(manifest.points))

    input_hashes = []
    completed = blocked = failed = 0
    quality_counts: dict[str, int] = {}
    calibration_level_counts: dict[str, int] = {}
    velocity_frame_counts: dict[str, int] = {}
    usable_fractions: list[float] = []
    baseline_rms_fractions: list[float] = []
    mask_reason_bin_counts: dict[str, int] = {}
    total_bins = 0
    point_summaries = []
    for point in manifest.points:
        outcome = reduce_point(point, manifest, config, calibration_profile, calibration_profile_path)
        session.log_event("POINT_PROCESSED", point_index=point.point_index, status=outcome.status,
                          reason=outcome.reason, timing=outcome.timing_seconds)
        if outcome.status == PointStatus.COMPLETED and outcome.master_spectrum is not None:
            completed += 1
            session.write_point(outcome.master_spectrum)
            spectrum = outcome.master_spectrum
            quality_counts[spectrum.quality.state] = quality_counts.get(spectrum.quality.state, 0) + 1
            calibration_level_counts[spectrum.calibration_level] = \
                calibration_level_counts.get(spectrum.calibration_level, 0) + 1
            velocity_frame_counts[spectrum.velocity_frame] = \
                velocity_frame_counts.get(spectrum.velocity_frame, 0) + 1
            if "usable_fraction" in spectrum.quality.metrics:
                usable_fractions.append(spectrum.quality.metrics["usable_fraction"])
            rms = spectrum.quality.metrics.get("baseline_fit_quality_rms_fraction")
            if rms is not None and rms == rms:  # not NaN
                baseline_rms_fractions.append(rms)
            input_hashes.append(spectrum.capture_refs[0].sha256)
            total_bins += spectrum.mask.shape[0]
            for reason_name in MaskFlag.__members__:
                if reason_name == "GOOD":
                    continue
                flag_value = MaskFlag[reason_name].value
                count = int(np.sum((spectrum.mask.astype(np.int64) & flag_value) != 0))
                mask_reason_bin_counts[reason_name] = mask_reason_bin_counts.get(reason_name, 0) + count
        elif outcome.status == PointStatus.BLOCKED:
            blocked += 1
        else:
            failed += 1
        point_summaries.append({"point_index": point.point_index, "status": outcome.status,
                                "reason": outcome.reason, "timing_seconds": outcome.timing_seconds})

    if completed == 0:
        status = "FAILED"
    elif blocked or failed:
        status = "PARTIAL"
    else:
        status = "COMPLETED"

    provenance = build_run_provenance(
        config, calibration_profile_hash=calibration_profile_hash, input_hashes=input_hashes,
        started_utc=started_utc)
    session.write_provenance(close_run_provenance(provenance))

    runtime_seconds = time.monotonic() - started
    manifest_dict = {
        "campaign_id": manifest.campaign_id, "reduce_session_id": session.session_id, "status": status,
        "points_discovered": len(manifest.points), "points_accepted": len(manifest.accepted_points()),
        "points_rejected": len(manifest.points) - len(manifest.accepted_points()),
        "points_completed": completed, "points_blocked": blocked, "points_failed": failed,
        "quality_counts": quality_counts, "runtime_seconds": runtime_seconds,
        "calibration_level_counts": calibration_level_counts, "velocity_frame_counts": velocity_frame_counts,
        "median_usable_fraction": float(np.median(usable_fractions)) if usable_fractions else None,
        "median_baseline_rms_fraction": float(np.median(baseline_rms_fractions)) if baseline_rms_fractions else None,
        "points": point_summaries, "source_campaign_root": str(manifest.root),
    }
    session.write_manifest(manifest_dict)

    # QC PRODUCTS (data-level only in V1 - no images, no new plotting
    # dependency added for this; see docs/REDUCE_V1_FREEZE.md known
    # limitations): mask occupancy, quality/calibration/velocity summaries.
    mask_occupancy_fraction = {name: count / total_bins for name, count in mask_reason_bin_counts.items()} \
        if total_bins else {}
    session.write_qc("mask_occupancy", {"bin_counts": mask_reason_bin_counts, "total_bins": total_bins,
                                        "occupancy_fraction": mask_occupancy_fraction})
    session.write_qc("quality_summary", {"quality_counts": quality_counts,
                                         "median_usable_fraction": manifest_dict["median_usable_fraction"],
                                         "median_baseline_rms_fraction": manifest_dict["median_baseline_rms_fraction"]})
    session.write_qc("calibration_summary", {"calibration_level_counts": calibration_level_counts})
    session.write_qc("velocity_summary", {"velocity_frame_counts": velocity_frame_counts})

    session.log_event("REDUCE_CAMPAIGN_END", status=status, runtime_seconds=runtime_seconds)

    return CampaignReduceReport(
        campaign_id=manifest.campaign_id, session_id=session.session_id, status=status,
        points_discovered=len(manifest.points), points_accepted=len(manifest.accepted_points()),
        points_rejected=len(manifest.points) - len(manifest.accepted_points()), points_completed=completed,
        points_blocked=blocked, points_failed=failed, quality_counts=quality_counts,
        calibration_level_counts=calibration_level_counts, velocity_frame_counts=velocity_frame_counts,
        median_usable_fraction=manifest_dict["median_usable_fraction"],
        median_baseline_rms_fraction=manifest_dict["median_baseline_rms_fraction"],
        runtime_seconds=runtime_seconds, output_dir=str(session.dir))
