"""ALMITA LOBE: measure MAIN's beam with a solar transit.

Two ways to move the Sun across the beam:

* drift (default): the mount points WEST of the Sun by the half range and tracking goes OFF. The mount stands still
  (fixed hour angle / declination) and the Earth's rotation carries the Sun through the beam and out the other side.
  Nothing moves while it measures; the background is measured at the same alt/az on both sides.
* scan: the mount steps in RA across the Sun (SOLAR tracking on), from -half range to +half range.

Every sample: one raw IQ capture kept on disk (HDF5, like every other ALMITA capture), the band power (linear), the
ADC clipping fraction, the SDR/LNA temperatures and the mount readback, appended to lobe_samples.csv as it happens
(the web page draws it live). Gain is fixed for the whole run and the RTL2832's own AGC is switched off explicitly
(rtl_tcp keeps whatever a previous client set - SDR++ turned it on on 2026-10-14). At the end (or on STOP) the
transit is fitted in LINEAR power with a Gaussian beam on a linear background, and the fit is written to
lobe_result.json.

Power metric: the 256-bin spectrum (QUICKLOOK resolution) without the DC bins and the band edges, passed through a
running median before averaging - narrow lines (MAIN's moving line, BL-072) do not leak into the continuum.

The expected beam FWHM is NOT measured yet (observer_config.json says 20 deg, alignment.py 14 deg, both
provisional): measuring it is the point. The half range is a multiple of that expectation, so the run is long
enough to record both sides and the background.
"""
import argparse, asyncio, csv, json, math, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from astropy_offline import configure_astropy_offline
configure_astropy_offline()
import astropy.units as u
from astropy.coordinates import AltAz, CIRS, EarthLocation, SkyCoord, get_sun
from astropy.time import Time

ROOT = Path(__file__).resolve().parent
NFFT = 256
EDGE_FRACTION = 0.12                 # each side: the RTL2832 decimation filter rolls off there
DC_BINS = 2                          # each side of the centre bin
MEDIAN_BINS = 15                     # running median that removes narrow lines before averaging
SOLAR_RA_RATE_DEG_PER_H = 15.0       # Sun in hour angle: 15.041 sidereal - 0.041 (Sun's own ~1 deg/day eastward)
DEFAULT_HALF_RANGE_FWHM = 1.5
MAX_HALF_RANGE_DEG = 60.0
CSV_FIELDS = ("index", "utc", "elapsed_s", "sun_ra_hours", "sun_dec_deg", "point_ra_hours", "point_dec_deg",
              "offset_deg", "separation_deg", "point_alt_deg", "sun_alt_deg", "power_lin", "power_db",
              "clipping_fraction", "adc_std_codes", "t_sdr_c", "t_lna_c", "tracking", "status", "hdf5")


# ------------------------------------------------------------------ pure pieces (tested)
def observer_location(config: Dict[str, Any]) -> EarthLocation:
    o = config["observer"]
    return EarthLocation(lat=o["latitude_deg"] * u.deg, lon=o["longitude_deg"] * u.deg, height=o["elevation_m"] * u.m)


def sun_eod(t: Time):
    """The Sun in the equatorial-of-date frame INDI EOD commands use (same as alignment.sun_eod)."""
    return get_sun(t).transform_to(CIRS(obstime=t))


def wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def offset_deg(sun_ra_h: float, sun_dec: float, point_ra_h: float) -> float:
    """Sun's hour angle minus the pointing's, as a sky angle along the RA circle (deg, cos(dec) applied).
    Negative: the Sun is still east of the beam (coming); 0: crossing; positive: it has passed (drift goes - to +)."""
    return wrap180((point_ra_h - sun_ra_h) * 15.0) * math.cos(math.radians(sun_dec))


def separation_deg(ra1_h: float, dec1: float, ra2_h: float, dec2: float) -> float:
    a1, d1, a2, d2 = map(math.radians, (ra1_h * 15, dec1, ra2_h * 15, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def half_range_deg(expected_fwhm_deg: float, half_range_fwhm: float) -> float:
    return min(MAX_HALF_RANGE_DEG, expected_fwhm_deg * half_range_fwhm)


def ra_offset_hours(sky_deg: float, dec_deg: float) -> float:
    """RA difference (hours) that spans `sky_deg` along a circle of declination `dec_deg`."""
    return sky_deg / (15.0 * max(0.05, math.cos(math.radians(dec_deg))))


def plan(mode: str, now: Time, location: EarthLocation, expected_fwhm_deg: float, half_range_fwhm: float,
         every_s: float, capture_s: float, scan_step_deg: float, settle_s: float, min_elevation_deg: float,
         scan_overhead_s: float = 8.0) -> Dict[str, Any]:
    """Where the mount points, how long it takes and whether every position stays above min_elevation_deg.
    Moves nothing; the web preview and the real run use this same function."""
    sun = sun_eod(now)
    sra, sdec = float(sun.ra.hour), float(sun.dec.deg)
    half = half_range_deg(expected_fwhm_deg, half_range_fwhm)
    out: Dict[str, Any] = {"mode": mode, "computed_utc": now.utc.isot + "Z", "expected_fwhm_deg": expected_fwhm_deg,
                           "half_range_fwhm": half_range_fwhm, "half_range_deg": half, "sun_ra_hours": sra, "sun_dec_deg": sdec,
                           "every_s": every_s, "capture_s": capture_s, "min_elevation_deg": min_elevation_deg, "problems": []}
    frame = lambda t: AltAz(obstime=t, location=location)  # noqa: E731
    sun_now = sun.transform_to(frame(now))
    out["sun_alt_deg"], out["sun_az_deg"] = float(sun_now.alt.deg), float(sun_now.az.deg)
    if mode == "drift":
        dra_h = ra_offset_hours(half, sdec)
        point_ra = (sra - dra_h) % 24.0                      # west of the Sun: the Sun drifts into the beam
        duration_s = 2 * dra_h * 15.0 / SOLAR_RA_RATE_DEG_PER_H * 3600.0
        point = SkyCoord(ra=point_ra * u.hourangle, dec=sdec * u.deg, frame=CIRS(obstime=now)).transform_to(frame(now))
        mid = now + (duration_s / 2) * u.s
        end = now + duration_s * u.s
        sun_mid = sun_eod(mid).transform_to(frame(mid))
        sun_end = sun_eod(end).transform_to(frame(end))
        out.update(point_ra_hours=point_ra, point_dec_deg=sdec, point_alt_deg=float(point.alt.deg), point_az_deg=float(point.az.deg),
                   duration_s=duration_s, samples_expected=int(duration_s // max(every_s, capture_s)) + 1,
                   crossing_utc=mid.utc.isot + "Z", end_utc=end.utc.isot + "Z",
                   sun_alt_at_crossing_deg=float(sun_mid.alt.deg), sun_alt_at_end_deg=float(sun_end.alt.deg))
        # tracking OFF holds hour angle and declination: the pointing keeps this alt/az for the whole run
        if point.alt.deg < min_elevation_deg:
            out["problems"].append(f"pointing altitude {point.alt.deg:.1f} deg is below the minimum {min_elevation_deg:.0f} deg")
        if sun_mid.alt.deg < min_elevation_deg:
            out["problems"].append(f"the Sun crosses the beam at altitude {sun_mid.alt.deg:.1f} deg, below {min_elevation_deg:.0f} deg")
    elif mode == "scan":
        n = int(round(2 * half / scan_step_deg)) + 1
        offsets = np.linspace(-half, half, n)
        per_point = capture_s + settle_s + scan_overhead_s
        duration_s = n * per_point
        times = now + (np.arange(n) * per_point) * u.s          # one vectorised transform for every position
        suns = sun_eod(times)
        ras = (suns.ra.hour + offsets / (15.0 * np.maximum(0.05, np.cos(suns.dec.radian)))) % 24.0
        altaz = SkyCoord(ra=ras * u.hourangle, dec=suns.dec, frame=CIRS(obstime=times)).transform_to(frame(times))
        positions = [{"offset_deg": float(x), "ra_hours": float(ra), "dec_deg": float(dec), "alt_deg": float(alt), "az_deg": float(az)}
                     for x, ra, dec, alt, az in zip(offsets, ras, suns.dec.deg, altaz.alt.deg, altaz.az.deg)]
        low = [f"{q['offset_deg']:+.1f}" for q in positions if q["alt_deg"] < min_elevation_deg]
        # SOLAR tracking holds each position for settle + capture: one whose hour angle passes OnStep's meridian limit
        # (+12 min) meanwhile makes OnStep stop and latch the error mid-scan (BL-060)
        from calibration_engine.hi_reference_selection import ONSTEP_MERIDIAN_LIMIT_MIN, SIDEREAL_RATE, hour_angle_hours
        lim = ONSTEP_MERIDIAN_LIMIT_MIN / 60.0
        ha0 = hour_angle_hours(ras, location, times)
        ha1 = ha0 + (settle_s + capture_s + scan_overhead_s) / 3600.0 * SIDEREAL_RATE
        crossing = [f"{x:+.1f}" for x, a0, a1 in zip(offsets, ha0, ha1) if a0 < lim <= a1]
        for q, h in zip(positions, ha0):
            q["hour_angle_h"] = float(h)
        if crossing:
            out["problems"].append(f"scan position(s) {', '.join(crossing)} would reach OnStep's meridian limit (+{ONSTEP_MERIDIAN_LIMIT_MIN:g} min) "
                                   "while held - OnStep would stop and latch the error; start a few minutes later")
        out.update(positions=positions, duration_s=duration_s, samples_expected=n, scan_step_deg=scan_step_deg,
                   point_ra_hours=positions[0]["ra_hours"], point_dec_deg=positions[0]["dec_deg"],
                   point_alt_deg=positions[0]["alt_deg"], point_az_deg=positions[0]["az_deg"],
                   end_utc=(now + duration_s * u.s).utc.isot + "Z")
        if low:
            out["problems"].append(f"{len(low)} scan positions fall below {min_elevation_deg:.0f} deg (offsets {', '.join(low[:6])}{'...' if len(low) > 6 else ''})")
    else:
        raise ValueError("mode must be drift or scan")
    return out


def _running_median(a: np.ndarray, k: int = MEDIAN_BINS) -> np.ndarray:
    p = k // 2
    return np.median(np.lib.stride_tricks.sliding_window_view(np.pad(a, (p, p), mode="edge"), k), axis=1)


def band_power(raw: np.ndarray) -> Dict[str, float]:
    """Continuum power of one capture (interleaved uint8 IQ): linear, arbitrary units (ADC codes^2 per bin)."""
    raw = np.asarray(raw, dtype=np.uint8)
    clipping = float(np.mean((raw <= 1) | (raw >= 254)))
    x = raw.astype(np.float32)
    z = (x[0::2] - 127.4) + 1j * (x[1::2] - 127.4)
    m = len(z) // NFFT
    if m < 4:
        raise ValueError("capture too short for the band power")
    sp = (np.abs(np.fft.fftshift(np.fft.fft(z[:m * NFFT].reshape(m, NFFT) * np.hanning(NFFT), axis=1), axes=1)) ** 2).mean(axis=0)
    edge = int(NFFT * EDGE_FRACTION)
    keep = np.ones(NFFT, dtype=bool)
    keep[:edge] = keep[NFFT - edge:] = False
    keep[NFFT // 2 - DC_BINS:NFFT // 2 + DC_BINS + 1] = False
    smooth = _running_median(sp)
    p = float(np.mean(smooth[keep]))
    return {"power_lin": p, "power_db": 10 * math.log10(p) if p > 0 else float("nan"), "clipping_fraction": clipping,
            "adc_std_codes": float(np.std(x[0::2]))}


def fit_transit(offsets: Sequence[float], power_lin: Sequence[float]) -> Dict[str, Any]:
    """Gaussian beam on a linear background, least squares in LINEAR power:
        P(x) = b0 + b1*x + A*exp(-4 ln2 (x-x0)^2 / W^2)
    For each (x0, W) on a grid the model is linear in (b0, b1, A): solved exactly, then the grid is refined. No
    scipy on this Pi. Returns the background-subtracted, peak-normalised profile too (what the page draws)."""
    x = np.asarray(offsets, dtype=float)
    y = np.asarray(power_lin, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    out: Dict[str, Any] = {"n": int(x.size), "model": "b0 + b1*x + A*exp(-4ln2(x-x0)^2/W^2), linear power"}
    if x.size < 8 or np.ptp(x) <= 0:
        out["status"] = "TOO FEW SAMPLES"
        return out
    span = float(np.ptp(x))

    def solve(x0s: np.ndarray, ws: np.ndarray):
        best = (np.inf, None)
        for w in ws:
            g = np.exp(-4 * math.log(2) * (x[None, :] - x0s[:, None]) ** 2 / w ** 2)       # (n_x0, n)
            for i, x0 in enumerate(x0s):
                M = np.column_stack([np.ones_like(x), x, g[i]])
                coef, *_ = np.linalg.lstsq(M, y, rcond=None)
                sse = float(np.sum((M @ coef - y) ** 2))
                if sse < best[0]:
                    best = (sse, (float(x0), float(w), coef))
        return best

    sse, (x0, w, coef) = solve(np.linspace(x.min(), x.max(), 41), np.geomspace(span / 40, span * 2, 41))
    sse, (x0, w, coef) = solve(np.linspace(x0 - span / 40, x0 + span / 40, 21), np.linspace(w * 0.9, w * 1.1, 21))
    b0, b1, a = (float(c) for c in coef)
    resid = y - (b0 + b1 * x + a * np.exp(-4 * math.log(2) * (x - x0) ** 2 / w ** 2))
    rms = float(np.sqrt(np.mean(resid ** 2)))
    out.update(x0_deg=x0, fwhm_deg=w, amplitude_lin=a, background_b0=b0, background_slope_per_deg=b1, residual_rms_lin=rms,
               snr=a / rms if rms > 0 else None, peak_over_background=a / (b0 + b1 * x0) if (b0 + b1 * x0) > 0 else None,
               span_deg=span)
    flags = []
    if a <= 0 or (rms > 0 and a / rms < 3):
        flags.append("NO CLEAR TRANSIT (amplitude < 3x residual)")
    if w > span / 1.5:
        flags.append("RANGE TOO SHORT: the fitted beam is wider than the measured span / 1.5 - both sides did not reach the background")
    if not (x.min() + 0.1 * span <= x0 <= x.max() - 0.1 * span):
        flags.append("PEAK AT THE EDGE of the measured range")
    out["flags"] = flags
    out["status"] = "FIT" if not flags else "FIT WITH WARNINGS"
    bg = b0 + b1 * x
    out["profile"] = {"offset_deg": x.tolist(), "normalized": ((y - bg) / a).tolist() if a > 0 else None}
    return out


def read_samples(csv_path: Path) -> List[Dict[str, Any]]:
    if not csv_path.is_file():
        return []
    rows = []
    with open(csv_path, newline="") as fh:
        for r in csv.DictReader(fh):
            row: Dict[str, Any] = {}
            for k, v in r.items():
                try:
                    row[k] = float(v) if k not in ("utc", "status", "hdf5", "tracking") else v
                except (TypeError, ValueError):
                    row[k] = None
            rows.append(row)
    return rows


def fit_samples(rows: List[Dict[str, Any]], max_clipping: float) -> Dict[str, Any]:
    use = [r for r in rows if r.get("status") == "VALID" and r.get("power_lin") and (r.get("clipping_fraction") or 0) <= max_clipping]
    return fit_transit([r["offset_deg"] for r in use], [r["power_lin"] for r in use])


# ------------------------------------------------------------------ the real run
class LobeRun:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.config = json.loads((ROOT / args.observer_config).read_text())
        self.location = observer_location(self.config)
        self.csv_path = self.out / "lobe_samples.csv"
        self.telescope = self.sdr = self.tuning = None
        sensors = self.config.get("temperature_sensors", {})
        self.temps = None
        if sensors:
            from temperature_sensors import DS18B20Reader
            self.temps = DS18B20Reader(sensors)
        self.t0 = time.monotonic()
        self.index = 0
        self.notes: List[str] = []

    def log(self, msg: str) -> None:
        print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC  {msg}", flush=True)

    async def connect(self) -> None:
        from indi_telescope_control import INDITelescopeControl
        from sdr_capture import SDRCapture
        import sdr_tuning
        a = self.args
        self.telescope = INDITelescopeControl(a.host, a.port, a.device, a.verbose)
        self.telescope.compact_console = True
        if not await self.telescope.connect():
            raise RuntimeError("INDI connection failed")
        self.sdr = SDRCapture("network", a.sdr_host, a.sdr_port, verbose=a.verbose)
        await self.sdr.connect()
        self.tuning = await sdr_tuning.tune_explicitly(self.sdr, a.center_freq, a.sample_rate, a.gain,
                                                       operating=sdr_tuning.operating_config())
        # Fixed gain is the whole point: tuner gain is manual and tune_explicitly() switches the RTL2832 digital AGC
        # off on this connection (BL-076) - refuse to measure if rtl_tcp says otherwise.
        if not str(self.tuning.get("rtl_agc", "")).startswith("off"):
            raise RuntimeError(f"RTL AGC not confirmed off: {self.tuning.get('rtl_agc')}")
        self.log(f"MAIN tuned {a.center_freq / 1e6:.6f} MHz, {a.sample_rate / 1e6:g} MS/s, gain {a.gain:.1f} dB fixed, RTL AGC {self.tuning['rtl_agc']}")

    def _sun(self):
        t = Time.now()
        s = sun_eod(t)
        return t, float(s.ra.hour), float(s.dec.deg), s

    def _alt(self, t: Time, ra_h: float, dec: float) -> float:
        return float(SkyCoord(ra=ra_h * u.hourangle, dec=dec * u.deg, frame=CIRS(obstime=t))
                     .transform_to(AltAz(obstime=t, location=self.location)).alt.deg)

    async def sample(self, writer, fh, tracking: str) -> Dict[str, Any]:
        import sdr_tuning
        a = self.args
        self.index += 1
        path = self.out / "iq" / f"lobe_{self.index:04d}.h5"
        path.parent.mkdir(exist_ok=True)
        row: Dict[str, Any] = {"index": self.index, "status": "CAPTURE_FAILED", "hdf5": str(path.relative_to(self.out)), "tracking": tracking}
        try:
            temps = self.temps.read_all() if self.temps else {}
            t, sra, sdec, _ = self._sun()
            await self.sdr.capture(a.capture_seconds, str(path), a.sample_rate,
                                   {**sdr_tuning.tuning_attrs(self.tuning), "gain_requested_db": a.gain,
                                    "lobe_mode": a.mode, "lobe_index": self.index, "sun_ra_hours": sra, "sun_dec_deg": sdec})
            pra, pdec = await self.telescope.get_coordinates(force_refresh=True)
            if pra is None or pdec is None:
                raise RuntimeError("mount readback failed")
            import h5py
            from sdr_capture import read_hdf5_iq_components
            with h5py.File(path, "r") as f:
                i_, q_ = read_hdf5_iq_components(f)
            raw = np.empty(i_.size * 2, dtype=np.uint8)
            raw[0::2], raw[1::2] = i_, q_
            m = band_power(raw)
            row.update(utc=t.utc.isot + "Z", elapsed_s=round(time.monotonic() - self.t0, 2), sun_ra_hours=sra, sun_dec_deg=sdec,
                       point_ra_hours=pra, point_dec_deg=pdec, offset_deg=offset_deg(sra, sdec, pra),
                       separation_deg=separation_deg(sra, sdec, pra, pdec), point_alt_deg=self._alt(t, pra, pdec),
                       sun_alt_deg=self._alt(t, sra, sdec), **m,
                       t_sdr_c=(temps.get("sdr") or {}).get("temperature_c"), t_lna_c=(temps.get("lna") or {}).get("temperature_c"),
                       status="VALID" if m["clipping_fraction"] <= a.max_clipping else "CLIPPED")
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            row["error"] = str(exc)
            self.log(f"sample {self.index}: {exc}")
        writer.writerow({k: row.get(k) for k in CSV_FIELDS})
        fh.flush()
        if row["status"] != "CAPTURE_FAILED":
            self.log(f"#{self.index:4d} offset {row['offset_deg']:+7.2f} deg  sep {row['separation_deg']:6.2f}  "
                     f"P {row['power_db']:7.3f} dB  clip {row['clipping_fraction']:.2%}  {row['status']}")
        return row

    async def ensure_tracking_off(self) -> str:
        state = await self.telescope.get_tracking_state(timeout=1.0)
        if state == "on":                   # OnStep sometimes switches it back on by itself (mount_stop.py, 2026-10-14)
            self.notes.append(f"{datetime.now(timezone.utc).isoformat()} tracking found ON during the drift: switched off again")
            self.log("tracking found ON - switching it off again")
            await self.telescope.set_tracking(False)
            await self.telescope.wait_tracking_state(False, timeout=5.0)
        return state

    async def run_drift(self, p: Dict[str, Any], writer, fh) -> None:
        a = self.args
        self.log(f"DRIFT: GOTO {p['point_ra_hours']:.4f} h {p['point_dec_deg']:+.3f} deg (west of the Sun by {p['half_range_deg']:.1f} deg)")
        if not await self.telescope.goto(p["point_ra_hours"], p["point_dec_deg"]):
            raise RuntimeError("GOTO failed")
        await asyncio.sleep(a.settle)
        await self.telescope.set_tracking(False)
        if not await self.telescope.wait_tracking_state(False, timeout=5.0):
            raise RuntimeError("tracking did not confirm OFF - the drift needs the mount still")
        self.log(f"tracking OFF; the Sun crosses around {p['crossing_utc']} and the run ends after {p['duration_s'] / 60:.1f} min")
        deadline = time.monotonic() + p["duration_s"] * 1.15 + 120
        while time.monotonic() < deadline:
            tick = time.monotonic()
            state = await self.ensure_tracking_off()
            row = await self.sample(writer, fh, state)
            if row.get("offset_deg") is not None and row["offset_deg"] >= p["half_range_deg"]:
                self.log("the Sun is past the half range: transit complete")
                return
            await asyncio.sleep(max(0.0, a.every - (time.monotonic() - tick)))
        self.notes.append("stopped by the time limit before the Sun reached +half range")

    async def run_scan(self, p: Dict[str, Any], writer, fh) -> None:
        a = self.args
        await self.telescope.set_track_mode("solar")
        if not await self.telescope.wait_track_mode("solar", timeout=5.0):
            raise RuntimeError("OnStep did not confirm TRACK_SOLAR")
        await self.telescope.set_tracking(True)
        for pos in p["positions"]:
            _, sra, sdec, _ = self._sun()                  # re-anchored on the Sun at each step
            ra = (sra + ra_offset_hours(pos["offset_deg"], sdec)) % 24.0
            if not await self.telescope.goto(ra, sdec):
                raise RuntimeError(f"GOTO failed at offset {pos['offset_deg']:+.1f} deg")
            await asyncio.sleep(a.settle)
            await self.sample(writer, fh, "on")
        await self.telescope.set_tracking(False)

    def write_result(self, p: Dict[str, Any], status: str, stop_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        rows = read_samples(self.csv_path)
        fit = fit_samples(rows, self.args.max_clipping)
        fit.pop("profile", None)
        res = {"status": status, "ended_utc": datetime.now(timezone.utc).isoformat(), "plan": p, "samples": len(rows),
               "valid": sum(1 for r in rows if r.get("status") == "VALID"), "clipped": sum(1 for r in rows if r.get("status") == "CLIPPED"),
               "gain_db": self.args.gain, "rtl_agc": (self.tuning or {}).get("rtl_agc"), "tuning": self.tuning, "fit": fit, "notes": self.notes,
               "stop_report": stop_report, "location": self.args.location_note}
        (self.out / "lobe_result.json").write_text(json.dumps(res, indent=2, default=str))
        return res

    async def run(self) -> int:
        a = self.args
        p = plan(a.mode, Time.now(), self.location, a.expected_fwhm, a.half_range_fwhm, a.every, a.capture_seconds,
                 a.scan_step, a.settle, a.min_elevation)
        (self.out / "lobe_plan.json").write_text(json.dumps(p, indent=2))
        self.log(f"PLAN {a.mode}: half range {p['half_range_deg']:.1f} deg (expected FWHM {a.expected_fwhm:g} x {a.half_range_fwhm:g}), "
                 f"duration {p['duration_s'] / 60:.1f} min, pointing alt {p['point_alt_deg']:.1f} deg")
        if p["problems"]:
            for q in p["problems"]:
                self.log(f"BLOCKED: {q}")
            return 3
        if a.dry_run:
            self.log("dry run: nothing moved")
            return 0
        status, stop_report = "INCOMPLETE", None
        fh = open(self.csv_path, "w", newline="")
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        fh.flush()
        try:
            await self.connect()
            await (self.run_drift(p, writer, fh) if a.mode == "drift" else self.run_scan(p, writer, fh))
            status = "COMPLETE"
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = "STOPPED"
            self.log("STOP received")
        except Exception as exc:  # noqa: BLE001 - the mount is stopped and the partial data saved whatever happened
            status = f"FAILED: {type(exc).__name__}: {exc}"
            self.log(status)
        finally:
            fh.close()
            if self.telescope is not None and status != "COMPLETE":
                import mount_stop
                try:
                    stop_report = await mount_stop.stop_mount_and_confirm(self.telescope, log=self.log)
                    self.log(mount_stop.summary_line(stop_report))
                except Exception as exc:  # noqa: BLE001
                    self.log(f"mount stop failed: {exc}")
            for closer in (self.sdr, self.telescope):
                try:
                    if closer is not None:
                        await (closer.close() if hasattr(closer, "close") else closer.disconnect())
                except Exception:  # noqa: BLE001
                    pass
        res = self.write_result(p, status, stop_report)
        f = res["fit"]
        if f.get("fwhm_deg") is not None:
            self.log(f"FIT {f['status']}: FWHM {f['fwhm_deg']:.2f} deg, peak at {f['x0_deg']:+.2f} deg, SNR {f.get('snr') or 0:.1f}"
                     + (f"; {'; '.join(f['flags'])}" if f.get("flags") else ""))
        else:
            self.log(f"FIT {f.get('status')}")
        self.log(f"Result {status}: {res['valid']} valid samples, {res['clipped']} clipped")
        return 0 if status == "COMPLETE" else 2


def parse_args(argv=None):
    import sdr_tuning
    cfg = json.loads((ROOT / "observer_config.json").read_text())
    p = argparse.ArgumentParser(description="ALMITA LOBE: beam from a solar transit (drift or scan)")
    p.add_argument("--mode", choices=("drift", "scan"), default="drift")
    p.add_argument("--expected-fwhm", type=float, default=float(cfg.get("observation_defaults", {}).get("beam_fwhm_deg")
                                                                   or cfg.get("beam_fwhm_deg") or 20.0),
                   help="expected beam FWHM (deg) - provisional; sets the half range")
    p.add_argument("--half-range-fwhm", type=float, default=DEFAULT_HALF_RANGE_FWHM, help="half range in units of the expected FWHM")
    p.add_argument("--every", type=float, default=5.0, help="seconds between samples (drift)")
    p.add_argument("--capture-seconds", type=float, default=1.0)
    p.add_argument("--scan-step", type=float, default=1.0, help="deg between scan positions")
    p.add_argument("--settle", type=float, default=2.0)
    p.add_argument("--gain", type=float, default=40.2, help="fixed for the whole run")
    p.add_argument("--min-elevation", type=float, default=10.0)
    p.add_argument("--max-clipping", type=float, default=0.001, help="samples above this ADC clipping fraction are kept but not fitted")
    p.add_argument("--center-freq", type=int, default=sdr_tuning.operating_center_frequency_hz())
    p.add_argument("--sample-rate", type=int, default=2_400_000)
    p.add_argument("--output-dir", default=f"data/lobe/LOBE-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    p.add_argument("--location-note", default="open sky (operator, 2026-10-15)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--host", default="localhost"); p.add_argument("--port", type=int, default=7624)
    p.add_argument("--device", default="LX200 OnStep"); p.add_argument("--sdr-host", default="localhost")
    p.add_argument("--sdr-port", type=int, default=1234); p.add_argument("--observer-config", default="observer_config.json")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    if not (1 <= a.expected_fwhm <= 90): p.error("--expected-fwhm must be 1..90 deg")
    if not (0.5 <= a.half_range_fwhm <= 4): p.error("--half-range-fwhm must be 0.5..4")
    if not (0.2 <= a.capture_seconds <= 10) or a.every < a.capture_seconds: p.error("--capture-seconds 0.2..10 and --every >= it")
    if not (0.2 <= a.scan_step <= 10): p.error("--scan-step must be 0.2..10 deg")
    if not (0 <= a.gain <= 50): p.error("--gain must be 0..50 dB")
    return a


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(LobeRun(args).run())
    except KeyboardInterrupt:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
