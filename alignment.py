#!/usr/bin/env python3
"""ALMITA Alignment V2: spherical Sun/HI multi-reference alignment."""

import argparse, asyncio, csv, json, math, re, subprocess, time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Tuple

import h5py
import numpy as np
from astropy_offline import configure_astropy_offline
configure_astropy_offline()
import astropy.units as u
from astropy.coordinates import AltAz, CIRS, EarthLocation, SkyCoord, SkyOffsetFrame, get_sun
from astropy.time import Time

from indi_telescope_control import INDITelescopeControl
from sdr_capture import SDRCapture, read_hdf5_iq_components
from temperature_sensors import DS18B20Reader
from hi_spectral_metric import (
    HI_REST_HZ as METRIC_HI_REST_HZ,
    compute_hi_metric_v2,
    dc_mask,
    detect_fixed_spurs,
    measure_dc_mask_half_width,
    robust_psd_from_iq,
)

PROVISIONAL_BEAM_FWHM_DEG = 14.0
HI_REST_HZ = METRIC_HI_REST_HZ
DEFAULT_GAIN_DB = 40.2
DEFAULT_SUN_GAIN_DB = 20.0
DEFAULT_CONFIDENCE_THRESHOLD = 0.65
HI_VELOCITY_WINDOW_KM_S = 200.0
HI_SNR_THRESHOLD = 5.0
DEFAULT_HI_INTEGRATION_SECONDS = 20.0
DEFAULT_MINIMUM_VALID_POSITIONS = 8
HI_INTEGRATION_VALIDATION_STATUS = "PROVISIONAL — OFFLINE EXTRAPOLATED NOT HARDWARE VALIDATED"


def sun_eod(obstime):
    """Return the Sun in the equatorial-of-date frame commanded by INDI EOD."""
    return get_sun(obstime).transform_to(CIRS(obstime=obstime))


def sync_allowed(requested, confidence, threshold=DEFAULT_CONFIDENCE_THRESHOLD):
    """SYNC is possible only after explicit authorization and sufficient confidence."""
    return bool(requested and confidence >= threshold)


@dataclass
class AlignmentEstimate:
    offset_ra_deg: float
    offset_dec_deg: float
    separation_deg: float
    confidence: float
    residual: float
    score: float
    samples: int


def load_hi_catalog(path):
    rows = list(csv.DictReader(Path(path).open()))
    if not rows:
        raise ValueError(f"HI catalog is empty: {path}")
    coordinates = SkyCoord(
        ra=[float(row["ra_hours"]) for row in rows] * u.hourangle,
        dec=[float(row["dec_deg"]) for row in rows] * u.deg,
    )
    return coordinates, np.asarray([float(row["tb_kelvin"]) for row in rows])


def gaussian_convolved_template(query, catalog_coords, values,
                                beam_fwhm_deg=PROVISIONAL_BEAM_FWHM_DEG):
    """Gaussian convolution evaluated with exact spherical separations."""
    sigma = beam_fwhm_deg / (2 * math.sqrt(2 * math.log(2)))
    query = SkyCoord(query).reshape((-1,))
    result = []
    for coordinate in query:
        distance = coordinate.separation(catalog_coords).deg
        weights = np.exp(-0.5 * (distance / sigma) ** 2)
        weights[distance > max(3.5 * sigma, 12.0)] = 0
        result.append(np.sum(weights * values) / np.sum(weights))
    return np.asarray(result)


class LocalSphericalTemplate:
    """Reusable local interpolation of a spherically convolved HI catalog."""
    def __init__(self, center, catalog_coords, values, beam_fwhm_deg, extent_deg=12.0,
                 step_deg=.3):
        self.center, self.extent, self.step = center, extent_deg, step_deg
        self.axis = np.arange(-extent_deg, extent_deg + step_deg / 2, step_deg)
        east, north = np.meshgrid(self.axis, self.axis)
        coordinates = offset_coordinates(center, east.ravel(), north.ravel())
        self.grid = gaussian_convolved_template(
            coordinates, catalog_coords, values, beam_fwhm_deg).reshape(east.shape)

    def __call__(self, coordinates):
        local = coordinates.transform_to(SkyOffsetFrame(origin=self.center))
        x = np.clip((local.lon.deg + self.extent) / self.step, 0, len(self.axis) - 1.000001)
        y = np.clip((local.lat.deg + self.extent) / self.step, 0, len(self.axis) - 1.000001)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        x1, y1 = np.minimum(x0 + 1, len(self.axis) - 1), np.minimum(y0 + 1, len(self.axis) - 1)
        dx, dy = x - x0, y - y0
        return ((1 - dx) * (1 - dy) * self.grid[y0, x0] +
                dx * (1 - dy) * self.grid[y0, x1] +
                (1 - dx) * dy * self.grid[y1, x0] + dx * dy * self.grid[y1, x1])


def offset_coordinates(center, east_deg, north_deg):
    frame = SkyOffsetFrame(origin=center)
    return SkyCoord(lon=np.asarray(east_deg) * u.deg,
                    lat=np.asarray(north_deg) * u.deg, frame=frame).icrs


def multiscale_pattern(center, radii=(5.0, 2.0, 0.6), ring_points=16):
    east, north = [0.0], [0.0]
    for radius in radii:
        for angle in np.linspace(0, 2 * np.pi, ring_points, endpoint=False):
            east.append(radius * np.cos(angle)); north.append(radius * np.sin(angle))
    return offset_coordinates(center, east, north)


def shifted_positions(positions, center, east, north):
    local = positions.transform_to(SkyOffsetFrame(origin=center))
    return offset_coordinates(center, local.lon.deg + east, local.lat.deg + north)


def _match(observed, expected):
    if len(observed) < 4 or np.std(observed) < 1e-12 or np.std(expected) < 1e-12:
        return -1.0, float("inf")
    score = float(np.corrcoef(observed, expected)[0, 1])
    design = np.column_stack((expected, np.ones(len(expected))))
    fit = design @ np.linalg.lstsq(design, observed, rcond=None)[0]
    residual = float(np.sqrt(np.mean((observed - fit) ** 2)) / np.std(observed))
    return score, residual


def estimate_template_offset(positions, observed, center, template,
                             search_levels=((5.5, .5), (1.0, .1), (.25, .025))):
    local = positions.transform_to(SkyOffsetFrame(origin=center))
    sample_east, sample_north = local.lon.deg, local.lat.deg
    best_east = best_north = 0.0
    best_score, best_residual = -2.0, float("inf")
    for radius, step in search_levels:
        origin_east, origin_north = best_east, best_north
        axis = np.arange(-radius, radius + step / 4, step)
        east_grid, north_grid = np.meshgrid(origin_east + axis, origin_north + axis)
        candidate_east, candidate_north = east_grid.ravel(), north_grid.ravel()
        all_east = (candidate_east[:, None] + sample_east[None, :]).ravel()
        all_north = (candidate_north[:, None] + sample_north[None, :]).ravel()
        coordinates = offset_coordinates(center, all_east, all_north)
        expected_grid = template(coordinates).reshape(len(candidate_east), len(observed))
        for index, expected in enumerate(expected_grid):
            score, residual = _match(observed, expected)
            if score > best_score or (math.isclose(score, best_score) and residual < best_residual):
                best_east, best_north = float(candidate_east[index]), float(candidate_north[index])
                best_score, best_residual = score, residual
    confidence = float(np.clip((best_score + 1) / 2 * math.exp(-.35 * best_residual), 0, 1))
    corrected = offset_coordinates(center, [best_east], [best_north])[0]
    return AlignmentEstimate(best_east, best_north,
                             float(center.separation(corrected).deg), confidence,
                             best_residual, best_score, len(observed))


def compute_hi_metric(iq_bytes, sample_rate, center_frequency_hz, fft_size=8192,
                      line_half_width_hz=250_000.0):
    """HI line excess over local spectral baseline; never full-band mean power."""
    raw = np.asarray(iq_bytes, dtype=np.uint8)
    clipping = float(np.mean((raw <= 1) | (raw >= 254)))
    iq = ((raw[0::2].astype(np.float32) - 127.5) +
          1j * (raw[1::2].astype(np.float32) - 127.5)) / 127.5
    count = len(iq) // fft_size
    if count < 2:
        raise ValueError("insufficient IQ for HI PSD")
    blocks = iq[:count * fft_size].reshape(count, fft_size) * np.hanning(fft_size)
    psd = np.median(np.abs(np.fft.fftshift(np.fft.fft(blocks, axis=1), axes=1)) ** 2, axis=0)
    frequency = center_frequency_hz + np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate))
    distance = np.abs(frequency - HI_REST_HZ)
    line = distance <= line_half_width_hz
    sides = (distance >= 1.5 * line_half_width_hz) & (distance <= min(.45 * sample_rate, 950_000))
    if not line.any() or not sides.any():
        raise ValueError("HI windows do not fit sampled band")
    baseline = float(np.median(psd[sides]))
    metric = float(np.mean(np.clip(psd[line] - baseline, 0, None)) / max(baseline, 1e-20))
    return {"metric": metric, "baseline": baseline, "clipping_fraction": clipping,
            "fft_segments": count}


def compute_sun_metric(iq_bytes, fft_size=8192):
    raw = np.asarray(iq_bytes, dtype=np.uint8)
    clipping = float(np.mean((raw <= 1) | (raw >= 254)))
    iq = ((raw[0::2].astype(np.float32) - 127.5) +
          1j * (raw[1::2].astype(np.float32) - 127.5)) / 127.5
    count = len(iq) // fft_size
    if count < 2:
        raise ValueError("insufficient IQ for Sun metric")
    blocks = iq[:count * fft_size].reshape(count, fft_size) * np.hanning(fft_size)
    power = np.abs(np.fft.fft(blocks, axis=1)) ** 2
    return {"metric": float(np.median(np.mean(power, axis=1))),
            "clipping_fraction": clipping, "fft_segments": count}


def choose_hi_region(catalog_coords, values, location, obstime, min_altitude, beam):
    altitude = catalog_coords.transform_to(AltAz(obstime=obstime, location=location)).alt.deg
    candidates = catalog_coords[altitude >= min_altitude]
    if not candidates.size:
        raise RuntimeError("no HI region above minimum altitude")
    best = None
    for center in candidates[::max(1, candidates.size // 180)]:
        probe = multiscale_pattern(center, (5.0, 2.0), 8)
        expected = gaussian_convolved_template(probe, catalog_coords, values, beam)
        contrast, signal = float(np.std(expected)), float(np.mean(expected))
        score = contrast * math.log1p(max(signal, 0))
        if best is None or score > best[0]:
            best = score, center, signal, contrast
    return best[1], {"selection_score": best[0], "template_mean": best[2],
                     "template_contrast": best[3]}


def synthetic_template(center, coordinates):
    local = coordinates.transform_to(SkyOffsetFrame(origin=center))
    x, y = local.lon.deg, local.lat.deg
    return (20 + 16 * np.exp(-((x - 2) ** 2 / 10 + (y + 1) ** 2 / 4))
            + 9 * np.exp(-((x + 3) ** 2 / 3 + (y - 2.5) ** 2 / 8))
            + .7 * x - .35 * y)


def simulate_offset_case(east, north, noise_fraction=.03, center=None, seed=1,
                         template=None):
    center = center or SkyCoord(ra=359 * u.deg, dec=-33 * u.deg)
    positions = multiscale_pattern(center)
    template = template or (lambda coords: synthetic_template(center, coords))
    clean = template(shifted_positions(positions, center, east, north))
    observed = clean + np.random.default_rng(seed).normal(0, noise_fraction * np.std(clean), len(clean))
    estimate = estimate_template_offset(positions, observed, center, template)
    truth = offset_coordinates(center, [east], [north])[0]
    recovered = offset_coordinates(center, [estimate.offset_ra_deg], [estimate.offset_dec_deg])[0]
    return estimate, {"injected_ra_deg": east, "injected_dec_deg": north,
                      "error_angular_deg": float(truth.separation(recovered).deg)}


def diagnostic_png(path, rows, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    axes[0].scatter([r["injected_ra_deg"] for r in rows], [r["injected_dec_deg"] for r in rows], label="injected")
    axes[0].scatter([r["offset_ra_deg"] for r in rows], [r["offset_dec_deg"] for r in rows], marker="x", label="recovered")
    axes[0].set(xlabel="east offset (deg)", ylabel="north offset (deg)"); axes[0].axis("equal"); axes[0].legend(); axes[0].grid()
    points = axes[1].scatter([r["magnitude_deg"] for r in rows], [r["error_angular_deg"] for r in rows], c=[r["confidence"] for r in rows])
    axes[1].set(xlabel="injected magnitude (deg)", ylabel="error (deg)"); axes[1].grid(); fig.colorbar(points, label="confidence")
    fig.suptitle(title); fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


def run_simulation(output_dir):
    catalog_coords, catalog_values = load_hi_catalog(Path("data/hi_sky_catalog_2000pts.csv"))
    # A structured, high-contrast catalog region selected by the same
    # contrast criterion used for field HI reference selection.
    center = SkyCoord(ra=315 * u.deg, dec=32.9268 * u.deg)
    template = LocalSphericalTemplate(center, catalog_coords, catalog_values,
                                      PROVISIONAL_BEAM_FWHM_DEG)
    cases = []
    for magnitude in (.5, 1, 2, 3, 5):
        for angle in (0, 45, 135, 225, 315):
            east, north = magnitude * math.cos(math.radians(angle)), magnitude * math.sin(math.radians(angle))
            estimate, truth = simulate_offset_case(east, north, seed=100 + len(cases),
                                                    center=center, template=template)
            case = {**truth, **asdict(estimate), "magnitude_deg": magnitude, "direction_deg": angle}
            # 0.35 deg is 2.5% of the provisional 14 deg beam and is the
            # offline field-readiness tolerance for this deliberately noisy,
            # catalog-resolution-limited simulation.
            case["success"] = case["error_angular_deg"] <= .35 and estimate.confidence >= .6
            cases.append(case)
    summary = {"cases": cases, "success_rate": float(np.mean([c["success"] for c in cases])),
               "mean_error_deg": float(np.mean([c["error_angular_deg"] for c in cases])),
               "max_error_deg": float(np.max([c["error_angular_deg"] for c in cases]))}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "alignment_result.json").write_text(json.dumps({"reference": "simulation", **summary}, indent=2))
    diagnostic_png(output_dir / "alignment_diagnostic.png", cases, "Alignment V2 synthetic recovery")
    return summary


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class AlignmentRunner:
    def __init__(self, args):
        self.args = args
        self.output_dir = Path(args.output_dir or f"data/alignment/{datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S')}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        config = json.loads(Path(args.observer_config).read_text())
        observer = config["observer"]
        self.location = EarthLocation(lat=observer["latitude_deg"] * u.deg,
                                      lon=observer["longitude_deg"] * u.deg,
                                      height=observer["elevation_m"] * u.m)
        self.catalog_coords, self.catalog_values = load_hi_catalog(args.catalog)
        sensor_config = config.get("temperature_sensors", {})
        self.temperature_reader = DS18B20Reader(sensor_config) if sensor_config else None
        self.telescope = self.sdr = None

    def resolve_reference(self):
        now = Time.now(); sun = sun_eod(now)
        sun_altitude = float(sun.transform_to(AltAz(obstime=now, location=self.location)).alt.deg)
        if self.args.reference == "sun":
            if sun_altitude <= 0:
                raise RuntimeError("Sun is astronomically below horizon")
            return "sun", sun, {"sun_altitude_deg": sun_altitude, "physical_visibility_assumed": False}
        if self.args.reference == "auto" and sun_altitude > 0:
            return "sun", sun, {"sun_altitude_deg": sun_altitude, "physical_visibility_assumed": False}
        center, info = choose_hi_region(self.catalog_coords, self.catalog_values, self.location,
                                        now, self.args.min_elevation, self.args.beam_fwhm)
        info["auto_fallback_from_sun"] = self.args.reference == "auto"
        return "hi", center, info

    @staticmethod
    def _capture_metadata(path):
        with h5py.File(path) as handle:
            if "iq_data" not in handle or handle.attrs.get("file_state") != "complete":
                raise ValueError("HDF5 is not a complete iq_data capture")
            if handle["iq_data"].ndim != 1 or handle["iq_data"].size % 2:
                raise ValueError("HDF5 IQ data must be a flat, even-length array")
            metadata = {key: value.item() if isinstance(value, np.generic) else value
                        for key, value in handle.attrs.items()}
            metadata["iq_complex_samples"] = handle["iq_data"].size // 2
        return metadata

    @classmethod
    def _read_capture(cls, path):
        metadata = cls._capture_metadata(path)
        with h5py.File(path) as handle:
            # iq_data is already canonical interleaved uint8. Reading it directly
            # avoids the second full-size allocation made by splitting and then
            # rebuilding I/Q components.
            raw = handle["iq_data"][:]
        return raw, metadata

    async def acquire(self, reference, positions):
        """Pass 1: acquire captures only; HI metrics wait for the full ensemble."""
        self.telescope = INDITelescopeControl(self.args.host, self.args.port, self.args.device, self.args.verbose)
        self.sdr = SDRCapture("network", self.args.sdr_host, self.args.sdr_port, verbose=self.args.verbose)
        if not await self.telescope.connect(): raise RuntimeError("INDI connection failed")
        await self.sdr.connect()
        gain = self.args.sun_gain if reference == "sun" else self.args.gain
        await self.sdr.configure(int(self.args.center_freq), self.args.sample_rate, gain=gain)
        duration = self.args.integration_seconds if reference == "hi" else (self.args.capture_time or 2.0)
        records = []
        for index, position in enumerate(positions, 1):
            path = self.output_dir / f"alignment_sample_{index:03d}.h5"
            record = {"index": index, "status": "CAPTURE_FAILED",
                      "commanded_ra_hours": position.ra.hour,
                      "commanded_dec_deg": position.dec.deg,
                      "mount_ra_hours": None, "mount_dec_deg": None,
                      "hdf5_path": str(path), "gain": gain,
                      "integration_seconds": duration}
            records.append(record)
            try:
                if not await self.telescope.goto(position.ra.hour, position.dec.deg):
                    raise RuntimeError("GOTO failed")
                await asyncio.sleep(self.args.settle)
                mount_ra, mount_dec = await self.telescope.get_coordinates(force_refresh=True)
                record.update(mount_ra_hours=mount_ra, mount_dec_deg=mount_dec)
                record["temperatures_pre"] = self.temperature_reader.read_all() if self.temperature_reader else {}
                record["capture_started_utc"] = datetime.now(timezone.utc).isoformat()
                await self.sdr.capture(duration, str(path), self.args.sample_rate,
                                       {"gain": gain, "alignment_reference": reference,
                                        "target_ra_hours": position.ra.hour,
                                        "target_dec_deg": position.dec.deg})
                record["capture_completed_utc"] = datetime.now(timezone.utc).isoformat()
                record["temperatures_post"] = self.temperature_reader.read_all() if self.temperature_reader else {}
                raw, metadata = self._read_capture(path)
                record["hdf5_metadata"] = metadata
                clipping = float(np.mean((raw <= 1) | (raw >= 254)))
                record.update(status="VALID", clipping_fraction=clipping,
                              iq_complex_samples=raw.size // 2)
                if clipping > self.args.max_clipping:
                    record.update(status="INVALID_HDF5", error=f"clipping guard: {clipping:.3%}")
            except (OSError, ValueError, KeyError) as exc:
                record.update(status="INVALID_HDF5", error=str(exc))
            except Exception as exc:
                record.update(status="CAPTURE_FAILED", error=str(exc))
        return records

    def replay_records(self):
        """Load immutable capture references without creating hardware objects."""
        source = Path(self.args.replay_dir).resolve()
        if self.output_dir.resolve() == source:
            raise ValueError("replay output directory must not overwrite its source")
        result_path = source / "alignment_result.json"
        if result_path.is_file():
            payload = json.loads(result_path.read_text())
            source_records = payload.get("measured_positions", [])
            center_data = payload["center_coordinates"]
            reference = payload.get("reference", "hi")
            selection = dict(payload.get("expected_template_metric", {}).get("selection", {}))
        else:
            paths = sorted(source.glob("alignment_sample_*.h5"))
            if not paths:
                raise ValueError("replay source has no alignment_result.json or captures")
            metadata_by_path = [(path, self._capture_metadata(path)) for path in paths]
            references = {str(metadata.get("alignment_reference", "hi"))
                          for _, metadata in metadata_by_path}
            if len(references) != 1 or references.pop() not in ("hi", "sun"):
                raise ValueError("captures have an inconsistent alignment reference")
            reference = str(metadata_by_path[0][1].get("alignment_reference", "hi"))
            targets_available = all("target_ra_hours" in metadata and
                                    "target_dec_deg" in metadata
                                    for _, metadata in metadata_by_path)
            if targets_available:
                commanded = [SkyCoord(ra=float(metadata["target_ra_hours"]) * u.hourangle,
                                      dec=float(metadata["target_dec_deg"]) * u.deg)
                             for _, metadata in metadata_by_path]
                mounts = [(None, None)] * len(paths)
                center = commanded[0]
                recovery_method = "hdf5_target_metadata"
            else:
                log_path = source / "alignment_execution.log"
                if not log_path.is_file():
                    raise ValueError("captures lack target coordinates and execution log is missing")
                mounts = self._capture_mount_positions(log_path)
                if len(mounts) != len(paths):
                    raise ValueError(
                        f"execution log has {len(mounts)} capture positions for {len(paths)} files")
                if reference == "sun":
                    first_timestamp = str(metadata_by_path[0][1].get("created_at", ""))
                    if not first_timestamp:
                        raise ValueError("SUN recovery requires a capture timestamp")
                    obstime = Time(datetime.fromisoformat(first_timestamp))
                    center = sun_eod(obstime)
                    recovery_method = "sun_ephemeris_at_first_capture"
                else:
                    observed_center = SkyCoord(ra=mounts[0][0] * u.hourangle,
                                               dec=mounts[0][1] * u.deg)
                    nearest = int(np.argmin(observed_center.separation(self.catalog_coords)))
                    center = self.catalog_coords[nearest]
                    recovery_method = "execution_log_first_mount_snapped_to_hi_catalog"
                pattern = list(multiscale_pattern(center))
                if len(paths) > len(pattern):
                    raise ValueError("capture count exceeds the alignment pattern")
                commanded = pattern[:len(paths)]
            source_records = []
            for (path, _), position, mount in zip(metadata_by_path, commanded, mounts):
                index = int(path.stem.rsplit("_", 1)[1])
                source_records.append({
                    "index": index,
                    "commanded_ra_hours": float(position.ra.hour),
                    "commanded_dec_deg": float(position.dec.deg),
                    "mount_ra_hours": mount[0],
                    "mount_dec_deg": mount[1],
                    "temperatures_pre": {},
                    "temperatures_post": {},
                })
            center_data = {"ra_hours": source_records[0]["commanded_ra_hours"],
                           "dec_deg": source_records[0]["commanded_dec_deg"]}
            selection = {"recovered_without_source_result": True,
                         "recovery_coordinate_method": recovery_method}
        records = []
        for source_record in source_records:
            record = dict(source_record)
            for legacy_key in ("metric", "baseline", "fft_segments",
                               "expected_template_metric"):
                record.pop(legacy_key, None)
            index = int(record["index"])
            path = source / f"alignment_sample_{index:03d}.h5"
            record.update(status="VALID", hdf5_path=str(path), error=None)
            try:
                metadata = self._capture_metadata(path)
                record["hdf5_metadata"] = metadata
                record["integration_seconds"] = float(metadata.get(
                    "requested_capture_duration_sec", metadata.get("duration_seconds", 0)))
            except (OSError, ValueError, KeyError) as exc:
                record.update(status="INVALID_HDF5", error=str(exc))
            records.append(record)
        center = SkyCoord(ra=center_data["ra_hours"] * u.hourangle,
                          dec=center_data["dec_deg"] * u.deg)
        selection["replay_source"] = str(source)
        return reference, center, selection, records

    @staticmethod
    def _capture_mount_positions(log_path):
        """Return the last fresh mount coordinate printed before each capture."""
        positions = []
        ra = dec = None
        for line in Path(log_path).read_text(errors="replace").splitlines():
            match = re.match(r"\s*RA=([-+0-9.eE]+)\s*$", line)
            if match:
                ra = float(match.group(1))
                continue
            match = re.match(r"\s*DEC=([-+0-9.eE]+)\s*$", line)
            if match:
                dec = float(match.group(1))
                continue
            if "Capturing" in line:
                if ra is None or dec is None:
                    raise ValueError("capture in execution log has no preceding coordinates")
                positions.append((ra, dec))
        return positions

    def analyze_hi_ensemble(self, records):
        """Pass 2: build one reference bandpass, then evaluate every valid capture."""
        started = time.perf_counter(); spectra, usable = [], []
        frequency = None; center_frequency = None; sample_rate = None
        psd_seconds = 0.0
        for record in records:
            if record.get("status") != "VALID":
                continue
            try:
                print(f"HI analysis pass 1/2: PSD {record['index']:03d}", flush=True)
                raw, metadata = self._read_capture(Path(record["hdf5_path"]))
                tick = time.perf_counter()
                current_frequency, psd = robust_psd_from_iq(
                    raw, float(metadata["sample_rate_hz"]),
                    float(metadata["center_frequency_hz"]))
                psd_seconds += time.perf_counter() - tick
                if frequency is not None and not np.array_equal(current_frequency, frequency):
                    raise ValueError("inconsistent frequency grid")
                frequency = current_frequency
                center_frequency = float(metadata["center_frequency_hz"])
                sample_rate = float(metadata["sample_rate_hz"])
                spectra.append(psd); usable.append(record)
                del raw
            except (OSError, ValueError, KeyError) as exc:
                record.update(status="INVALID_HDF5", error=str(exc))
        if len(usable) < self.args.minimum_valid_positions:
            return None, {"ensemble_positions_count": len(usable),
                          "status": "INSUFFICIENT VALID POSITIONS",
                          "psd_seconds": psd_seconds,
                          "total_seconds": time.perf_counter() - started}
        ensemble_started = time.perf_counter()
        spectra = np.asarray(spectra)
        reference = np.median(spectra, axis=0)
        dc_info = measure_dc_mask_half_width(frequency, reference, center_frequency)
        excluded_dc = dc_mask(frequency, center_frequency, dc_info["half_width_hz"])
        _, spur_items = detect_fixed_spurs(frequency, spectra, excluded_mask=excluded_dc,
                                            smooth_bins=101, z_threshold=8.0,
                                            persistence_threshold=.70, pad_bins=2,
                                            max_cluster_bins=25)
        spur_ranges = [(float(frequency[item["lo_bin"]]),
                        float(frequency[item["hi_bin"]])) for item in spur_items]
        ensemble_seconds = time.perf_counter() - ensemble_started
        metric_started = time.perf_counter()
        for record in usable:
            try:
                print(f"HI analysis pass 2/2: metric {record['index']:03d}", flush=True)
                raw, _ = self._read_capture(Path(record["hdf5_path"]))
                metric, _ = compute_hi_metric_v2(
                    raw, sample_rate, center_frequency,
                    velocity_window_km_s=HI_VELOCITY_WINDOW_KM_S,
                    dc_mask_half_width_hz=dc_info["half_width_hz"],
                    spur_ranges_hz=spur_ranges, reference_bandpass=reference)
                values = asdict(metric)
                record.update(status="VALID", metric=metric.metric_value,
                              metric_value=metric.metric_value,
                              metric_uncertainty=metric.metric_uncertainty,
                              metric_snr=metric.metric_snr, **{
                                  key: value for key, value in values.items()
                                  if key not in ("metric_value", "metric_uncertainty", "metric_snr")})
                del raw
            except (ValueError, FloatingPointError) as exc:
                record.update(status="METRIC_FAILED", error=str(exc))
        metric_seconds = time.perf_counter() - metric_started
        spur_json = [{"frequency_hz": item["frequency_hz"],
                      "start_frequency_hz": lo, "end_frequency_hz": hi,
                      "width_hz": hi - lo + dc_info["frequency_bin_hz"],
                      "persistence": item["persistence"], "score": item["score"]}
                     for item, (lo, hi) in zip(spur_items, spur_ranges)]
        valid = [record for record in records if record.get("status") == "VALID"]
        metadata = {"ensemble_positions_count": len(valid),
                    "ensemble_reference_method": "ensemble_reference_scaled_linear",
                    "center_frequency_hz": center_frequency,
                    "sample_rate_hz": sample_rate,
                    "dc_mask_half_width_hz": dc_info["half_width_hz"],
                    "dc_mask_bins": dc_info["half_width_bins"],
                    "dc_mask": dc_info, "dc_mask_measurement": dc_info,
                    "spur_masks": spur_json, "detected_spurs": spur_json,
                    "velocity_frame": "TOPOCENTRIC",
                    "velocity_window_kms": HI_VELOCITY_WINDOW_KM_S,
                    "psd_seconds": psd_seconds, "ensemble_seconds": ensemble_seconds,
                    "metric_evaluation_seconds": metric_seconds,
                    "total_seconds": time.perf_counter() - started,
                    "status": "PASS" if len(valid) >= self.args.minimum_valid_positions
                    else "INSUFFICIENT VALID POSITIONS"}
        return reference, metadata

    def replay_comparison(self, records):
        if not self.args.replay_dir:
            return None
        expected_path = Path(self.args.replay_dir) / "analysis_v2" / "hi_metric_v2_49.csv"
        if not expected_path.is_file():
            return {"status": "REFERENCE CSV NOT AVAILABLE", "mismatches": None}
        expected = {int(row["point"]): row for row in csv.DictReader(expected_path.open())}
        rows, differences = [], []
        for record in records:
            if record.get("status") != "VALID" or record["index"] not in expected:
                continue
            source = expected[record["index"]]
            row = {"point": record["index"]}
            mismatch = False
            for current_key, expected_key in (("metric_value", "metric_v2"),
                                               ("metric_uncertainty", "uncertainty"),
                                               ("metric_snr", "snr"),
                                               ("valid_fraction", "valid_fraction")):
                actual, wanted = float(record[current_key]), float(source[expected_key])
                difference = abs(actual - wanted); differences.append(difference)
                row.update({f"alignment_{current_key}": actual,
                            f"offline_{current_key}": wanted,
                            f"abs_difference_{current_key}": difference})
                mismatch |= difference > self.args.replay_tolerance
            row["mismatch"] = mismatch; rows.append(row)
        if rows:
            with (self.output_dir / "alignment_metric_v2_replay_comparison.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader(); writer.writerows(rows)
        return {"status": "PASS" if rows and not any(row["mismatch"] for row in rows) else "FAIL",
                "rows": len(rows), "tolerance": self.args.replay_tolerance,
                "max_abs_difference": max(differences) if differences else None,
                "median_abs_difference": float(np.median(differences)) if differences else None,
                "mismatches": sum(row["mismatch"] for row in rows)}

    def analyze_sun_records(self, records):
        for record in records:
            if record.get("status") != "VALID": continue
            try:
                raw, _ = self._read_capture(Path(record["hdf5_path"]))
                metric = compute_sun_metric(raw)
                record.update(**metric)
            except (OSError, ValueError, KeyError) as exc:
                record.update(status="METRIC_FAILED", error=str(exc))

    async def run(self):
        if self.args.replay_dir:
            reference, center, selection, replay_records = self.replay_records()
        else:
            reference, center, selection = self.resolve_reference()
            replay_records = None
        print("ALMITA ALIGNMENT V2"); print(f"Reference      {reference.upper()}")
        print(f"Beam           {self.args.beam_fwhm:.1f} deg provisional")
        print("Physical mask  NOT inferred")
        positions = multiscale_pattern(center)
        if self.args.dry_run:
            self.save(reference, center, [], None, selection, False, "PASS", {})
            print("SYNC           NO\nResult         PASS (DRY RUN)"); return 0
        try:
            if reference == "hi":
                records = replay_records if replay_records is not None else await self.acquire(reference, positions)
                _, analysis = self.analyze_hi_ensemble(records)
                analysis["metric_version"] = "2"
                analysis["metric_algorithm"] = "signed_fractional_residual_integrated_km_s"
                analysis["integration_seconds"] = (float(np.median([
                    record.get("integration_seconds", self.args.integration_seconds)
                    for record in records if record.get("status") == "VALID"]))
                    if any(record.get("status") == "VALID" for record in records)
                    else self.args.integration_seconds)
                analysis["integration_validation_status"] = (
                    "REPLAY OF RECORDED HARDWARE INTEGRATION" if self.args.replay_dir
                    else HI_INTEGRATION_VALIDATION_STATUS)
                analysis["integration_seconds_candidate"] = DEFAULT_HI_INTEGRATION_SECONDS
                analysis["integration_candidate_validation_status"] = HI_INTEGRATION_VALIDATION_STATUS
                analysis["template_type"] = "SYNTHETIC"
                analysis["template_ground_truth"] = False
                analysis["sync_eligible"] = False
                analysis["snr_threshold"] = HI_SNR_THRESHOLD
                analysis["replay_mode"] = bool(self.args.replay_dir)
                analysis["replay_comparison"] = (self.replay_comparison(records)
                                                   if analysis.get("status") == "PASS"
                                                   else None)
                valid = [record for record in records if record.get("status") == "VALID"]
                robust = [record for record in valid
                          if abs(record.get("metric_snr", 0)) >= HI_SNR_THRESHOLD]
                estimate = None
                if len(valid) < self.args.minimum_valid_positions:
                    status = "INSUFFICIENT VALID POSITIONS"
                elif len(robust) < self.args.minimum_robust_positions:
                    status = "NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE"
                else:
                    valid_positions = SkyCoord(
                        ra=[r["commanded_ra_hours"] for r in valid] * u.hourangle,
                        dec=[r["commanded_dec_deg"] for r in valid] * u.deg)
                    observed = np.asarray([r["metric_value"] for r in valid])
                    template = LocalSphericalTemplate(center, self.catalog_coords,
                                                      self.catalog_values, self.args.beam_fwhm)
                    estimate = estimate_template_offset(valid_positions, observed, center, template)
                    expected_best = template(shifted_positions(
                        valid_positions, center, estimate.offset_ra_deg, estimate.offset_dec_deg))
                    for record, expected in zip(valid, expected_best):
                        record["expected_template_metric"] = float(expected)
                    status = "NON-OBSERVATIONAL TEMPLATE — SYNC BLOCKED"
                if self.args.apply_sync:
                    print("SYNC           BLOCKED: HI TEMPLATE IS NON-OBSERVATIONAL")
                self.save(reference, center, records, estimate, selection, False, status, analysis)
                if estimate: self.print_result(estimate, False, status)
                else:
                    print(f"Samples        {len(valid)}\nRobust |SNR|≥5 {len(robust)}")
                    print(f"SYNC           NO\nResult         {status}")
                if self.args.replay_dir and status in (
                        "NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE",
                        "NON-OBSERVATIONAL TEMPLATE — SYNC BLOCKED"):
                    return 0
                return 2
            else:
                records = replay_records if replay_records is not None else await self.acquire(reference, positions)
                self.analyze_sun_records(records)
                valid = [record for record in records if record.get("status") == "VALID"]
                if len(valid) < self.args.minimum_valid_positions:
                    self.save(reference, center, records, None, selection, False,
                              "INSUFFICIENT VALID POSITIONS", {})
                    return 2
                valid_positions = SkyCoord(
                    ra=[r["commanded_ra_hours"] for r in valid] * u.hourangle,
                    dec=[r["commanded_dec_deg"] for r in valid] * u.deg)
                observed = np.asarray([r["metric"] for r in valid])
                sigma = self.args.beam_fwhm / (2 * math.sqrt(2 * math.log(2)))
                template = lambda coords: np.exp(-.5 * (coords.separation(center).deg / sigma) ** 2)
            estimate = estimate_template_offset(valid_positions, observed, center, template)
            expected_best = template(shifted_positions(
                valid_positions, center, estimate.offset_ra_deg, estimate.offset_dec_deg))
            for record, expected in zip(valid, expected_best):
                record["expected_template_metric"] = float(expected)
            status = "PASS" if estimate.confidence >= self.args.confidence_threshold else "LOW CONFIDENCE"
            sync_applied = False
            if self.args.apply_sync:
                if not sync_allowed(True, estimate.confidence, self.args.confidence_threshold):
                    print("SYNC           BLOCKED BY CONFIDENCE GUARD")
                else:
                    # If actual sky = commanded + estimated offset, command the
                    # inverse offset so the beam is physically on the reference.
                    compensated = offset_coordinates(
                        center, [-estimate.offset_ra_deg], [-estimate.offset_dec_deg])[0]
                    if not await self.telescope.goto(compensated.ra.hour, compensated.dec.deg):
                        raise RuntimeError("pre-SYNC compensated GOTO failed")
                    await asyncio.sleep(self.args.settle)
                    pre = await self.telescope.get_coordinates(force_refresh=True)
                    sync_applied = bool(await self.telescope.sync(center.ra.hour, center.dec.deg))
                    post = await self.telescope.get_coordinates(force_refresh=True)
                    selection["sync_coordinates"] = {"pre": pre, "post": post,
                        "compensated_command_ra_hours": compensated.ra.hour,
                        "compensated_command_dec_deg": compensated.dec.deg,
                        "reference_ra_hours": center.ra.hour, "reference_dec_deg": center.dec.deg}
                    if sync_applied:
                        outside = offset_coordinates(center, [2.0], [0.0])[0]
                        await self.telescope.goto(outside.ra.hour, outside.dec.deg)
                        await asyncio.sleep(self.args.settle)
                        await self.telescope.goto(center.ra.hour, center.dec.deg)
                        await asyncio.sleep(self.args.settle)
                        repeat_ra, repeat_dec = await self.telescope.get_coordinates(force_refresh=True)
                        repeat = SkyCoord(ra=repeat_ra * u.hourangle, dec=repeat_dec * u.deg)
                        selection["post_sync_repeatability"] = {
                            "ra_hours": repeat_ra, "dec_deg": repeat_dec,
                            "residual_deg": float(center.separation(repeat).deg),
                        }
            self.save(reference, center, records, estimate, selection, sync_applied, status, {})
            self.print_result(estimate, sync_applied, status)
            return 0 if status == "PASS" else 2
        finally:
            if self.sdr: await self.sdr.close()
            if self.telescope: await self.telescope.disconnect()

    def save(self, reference, center, records, estimate, selection, sync_applied, status,
             analysis_metadata=None):
        analysis_metadata = dict(analysis_metadata or {})
        if "status" in analysis_metadata:
            analysis_metadata["ensemble_status"] = analysis_metadata.pop("status")
        result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "git_commit": git_commit(),
                  "reference": reference, "center_coordinates": {"ra_hours": center.ra.hour, "dec_deg": center.dec.deg},
                  "measured_positions": records,
                  "measured_hi_metric": [r["metric"] for r in records
                                           if reference == "hi" and r.get("status") == "VALID"
                                           and "metric" in r],
                  "expected_template_metric": {"selection": selection,
                      "sample_values": [r.get("expected_template_metric") for r in records]},
                  "offsets": asdict(estimate) if estimate else None,
                  "confidence": estimate.confidence if estimate else None, "residual": estimate.residual if estimate else None,
                  "gain": self.args.sun_gain if reference == "sun" else self.args.gain,
                  "temperatures": [{"pre": r.get("temperatures_pre"), "post": r.get("temperatures_post")} for r in records],
                  "beam_fwhm_deg": self.args.beam_fwhm, "beam_provisional": True,
                  "sync_applied": sync_applied, "sync_executed": sync_applied,
                  "status": status, "result_status": status,
                  **analysis_metadata}
        (self.output_dir / "alignment_result.json").write_text(json.dumps(result, indent=2))
        self.result_png(center, records, estimate)

    def result_png(self, center, records, estimate):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
        if records:
            valid = [r for r in records if r.get("status") == "VALID" and "metric" in r]
            if valid:
                coords = SkyCoord(ra=[r["commanded_ra_hours"] for r in valid] * u.hourangle,
                                  dec=[r["commanded_dec_deg"] for r in valid] * u.deg)
                local = coords.transform_to(SkyOffsetFrame(origin=center))
                points = axes[0].scatter(local.lon.deg, local.lat.deg,
                                         c=[r["metric"] for r in valid], cmap="viridis")
                fig.colorbar(points, ax=axes[0], label="measured differential metric")
                axes[1].errorbar([r["index"] for r in valid], [r["metric"] for r in valid],
                                 yerr=[r.get("metric_uncertainty", 0) for r in valid],
                                 marker="o", linestyle="none", label="metric ± uncertainty")
                axes[2].bar([r["index"] for r in valid], [r.get("metric_snr", 0) for r in valid])
            axes[2].axhline(HI_SNR_THRESHOLD, color="red", linestyle="--")
            axes[2].axhline(-HI_SNR_THRESHOLD, color="red", linestyle="--")
            invalid = [r for r in records if r.get("status") != "VALID"]
            if invalid:
                axes[2].scatter([r["index"] for r in invalid], np.zeros(len(invalid)),
                                marker="x", color="black", label="invalid")
            if valid and all(r.get("expected_template_metric") is not None for r in valid):
                expected = np.asarray([r["expected_template_metric"] for r in valid])
                measured = np.asarray([r["metric"] for r in valid])
                design = np.column_stack((expected, np.ones(len(expected))))
                scaled = design @ np.linalg.lstsq(design, measured, rcond=None)[0]
                axes[1].plot([r["index"] for r in valid], scaled, marker=".",
                             label="SYNTHETIC template (not ground truth)")
                axes[1].legend()
        else:
            axes[0].scatter([0], [0], marker="*", s=120, label="reference")
            axes[1].text(.5, .5, "dry run: no measurements", ha="center")
        if estimate:
            axes[0].scatter([estimate.offset_ra_deg], [estimate.offset_dec_deg], marker="x", s=120,
                            color="red", label="best offset")
            axes[1].set_title(f"residual={estimate.residual:.3f} confidence={estimate.confidence:.3f}")
        axes[0].set(xlabel="tangent east / RA (deg)", ylabel="tangent north / DEC (deg)", title="Measured points / best offset")
        axes[0].grid()
        if axes[0].get_legend_handles_labels()[0]:
            axes[0].legend()
        axes[1].set(xlabel="sample", ylabel="metric", title="Measured profile")
        axes[2].set(xlabel="sample", ylabel="SNR", title="Differential significance (±5 threshold)")
        axes[1].grid(); axes[2].grid(); fig.tight_layout(); fig.savefig(self.output_dir / "alignment_diagnostic.png", dpi=150); plt.close(fig)

    @staticmethod
    def print_result(e, sync, status):
        print(f"Samples        {e.samples}\nOffset RA      {e.offset_ra_deg:+.3f} deg (tangent east)")
        print(f"Offset DEC     {e.offset_dec_deg:+.3f} deg (tangent north)\nSeparation     {e.separation_deg:.3f} deg")
        print(f"Confidence     {e.confidence:.3f}\nResidual       {e.residual:.3f}")
        print(f"SYNC           {'APPLIED' if sync else 'NO'}\nResult         {status}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="ALMITA Alignment V2 multi-reference")
    p.add_argument("--reference", choices=("sun", "hi", "auto"), default="auto")
    p.add_argument("--simulate", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply-sync", action="store_true"); p.add_argument("--no-sync", action="store_true")
    p.add_argument("--gain", type=float, default=DEFAULT_GAIN_DB); p.add_argument("--sun-gain", type=float, default=DEFAULT_SUN_GAIN_DB)
    p.add_argument("--beam-fwhm", type=float, default=PROVISIONAL_BEAM_FWHM_DEG)
    p.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    p.add_argument("--capture-time", type=float); p.add_argument("--integration-seconds", type=float)
    p.add_argument("--settle", type=float, default=2)
    p.add_argument("--min-elevation", type=float, default=20); p.add_argument("--max-clipping", type=float, default=.01)
    p.add_argument("--center-freq", type=int, default=round(HI_REST_HZ)); p.add_argument("--sample-rate", type=int, default=2_400_000)
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=7624)
    p.add_argument("--device", default="LX200 OnStep"); p.add_argument("--sdr-host", default="localhost")
    p.add_argument("--sdr-port", type=int, default=1234); p.add_argument("--observer-config", default="observer_config.json")
    p.add_argument("--catalog", default="data/hi_sky_catalog_2000pts.csv"); p.add_argument("--output-dir")
    p.add_argument("--replay-dir")
    p.add_argument("--minimum-valid-positions", type=int, default=DEFAULT_MINIMUM_VALID_POSITIONS)
    p.add_argument("--minimum-robust-positions", type=int, default=4)
    p.add_argument("--replay-tolerance", type=float, default=1e-9)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.no_sync: args.apply_sync = False
    if args.integration_seconds is None:
        args.integration_seconds = (args.capture_time if args.capture_time is not None
                                    else DEFAULT_HI_INTEGRATION_SECONDS)
    if args.beam_fwhm <= 0 or args.gain < 0 or args.sun_gain < 0: p.error("beam/gains must be fixed valid values")
    if args.integration_seconds <= 0 or (args.capture_time is not None and args.capture_time <= 0):
        p.error("integration/capture time must be positive")
    if args.minimum_valid_positions < 4 or args.minimum_robust_positions < 2:
        p.error("minimum position guards are too small")
    if args.replay_dir:
        args.apply_sync = False
    return args


async def async_main(args):
    if args.simulate:
        summary = run_simulation(Path(args.output_dir or "data/alignment/simulation"))
        print("ALMITA ALIGNMENT V2\nReference      SIMULATION")
        print(f"Samples        {len(summary['cases'])}\nSuccess rate   {summary['success_rate']:.1%}")
        print(f"Mean error     {summary['mean_error_deg']:.3f} deg\nMax error      {summary['max_error_deg']:.3f} deg")
        print(f"Result         {'PASS' if summary['success_rate'] >= .95 else 'FAIL'}")
        return 0 if summary["success_rate"] >= .95 else 2
    return await AlignmentRunner(args).run()


def main(argv=None): return asyncio.run(async_main(parse_args(argv)))
if __name__ == "__main__": raise SystemExit(main())
