#!/usr/bin/env python3
"""SUN/OFF gain sweep for Alignment V2. Never performs SYNC or a spatial scan."""

import argparse
import asyncio
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import astropy.units as u
import h5py
import numpy as np
from astropy.coordinates import AltAz, SkyCoord, SkyOffsetFrame
from astropy.time import Time

from alignment import sun_eod
from indi_telescope_control import INDITelescopeControl
from sdr_capture import SDRCapture
from temperature_sensors import DS18B20Reader


GAINS = (20.0, 28.0, 32.8, 36.4, 40.2)
SEQUENCE = ("SUN", "OFF", "SUN", "OFF", "SUN")


def robust_broadband_metrics(path, fft_size=8192):
    with h5py.File(path) as handle:
        raw = handle["iq_data"][:]
        sample_rate = float(handle.attrs["sample_rate_hz"])
        center_frequency = float(handle.attrs["center_frequency_hz"])
    i = raw[0::2].astype(np.float32)
    q = raw[1::2].astype(np.float32)
    iq = (i - np.mean(i)) + 1j * (q - np.mean(q))
    count = len(iq) // fft_size
    if count < 8:
        raise ValueError("insufficient samples for broadband PSD")
    window = np.hanning(fft_size).astype(np.float32)
    psd = np.zeros(fft_size, np.float64)
    chunk_segments = 64
    for start in range(0, count, chunk_segments):
        block = iq[start * fft_size:min(count, start + chunk_segments) * fft_size]
        block = block.reshape(-1, fft_size) * window
        transformed = np.fft.fftshift(np.fft.fft(block, axis=1), axes=1)
        psd += np.sum(np.abs(transformed) ** 2, axis=0)
    psd /= count
    frequency = center_frequency + np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate))
    distance = np.abs(frequency - center_frequency)
    mask = np.ones(fft_size, bool)
    edge = max(2, int(.02 * fft_size)); mask[:edge] = False; mask[-edge:] = False
    mask &= distance > 10_000
    log_psd = np.log(np.maximum(psd, 1e-30))
    median = np.median(log_psd[mask])
    mad = max(1.4826 * np.median(np.abs(log_psd[mask] - median)), 1e-9)
    spur = mask & (log_psd > median + 8 * mad)
    mask &= ~spur
    usable = psd[mask]
    subbands = [float(np.median(part)) for part in np.array_split(usable, 16) if len(part)]
    return {
        "iq_i_mean": float(np.mean(i)), "iq_q_mean": float(np.mean(q)),
        "iq_i_std": float(np.std(i)), "iq_q_std": float(np.std(q)),
        "p001": float(np.percentile(raw, .1)), "p999": float(np.percentile(raw, 99.9)),
        "clipping_fraction": float(np.mean((raw <= 1) | (raw >= 254))),
        "broadband_power": float(np.median(usable)),
        "broadband_mean_power": float(np.mean(usable)),
        "psd_bins_used": int(mask.sum()), "spur_bins_excluded": int(spur.sum()),
        "subband_power": subbands,
    }


def summarize_gain(records):
    sun = [r for r in records if r["position"] == "SUN" and r["status"] == "VALID"]
    off = [r for r in records if r["position"] == "OFF" and r["status"] == "VALID"]
    if len(sun) < 3 or len(off) < 2:
        return {"status": "INSUFFICIENT_VALID_CAPTURES", "snr": None}
    s = np.asarray([r["broadband_power"] for r in sun]); o = np.asarray([r["broadband_power"] for r in off])
    difference = float(np.mean(s) - np.mean(o))
    uncertainty = float(math.sqrt(np.var(s, ddof=1) / len(s) + np.var(o, ddof=1) / len(o)))
    snr = difference / max(uncertainty, 1e-30)
    sun_sub = np.asarray([r["subband_power"] for r in sun]); off_sub = np.asarray([r["subband_power"] for r in off])
    sub_ratio = np.mean(sun_sub, axis=0) / np.maximum(np.mean(off_sub, axis=0), 1e-30) - 1
    max_clip = max(r["clipping_fraction"] for r in sun + off)
    adc_margin = min(r["p001"] for r in sun + off) > 2 and max(r["p999"] for r in sun + off) < 253
    repeatability = float(np.std(s, ddof=1) / max(abs(np.mean(s)), 1e-30))
    broadband_fraction_positive = float(np.mean(sub_ratio > 0))
    passed = bool(snr >= 5 and difference > 0 and max_clip <= 1e-4 and adc_margin
                  and broadband_fraction_positive >= .75)
    return {
        "status": "PASS" if passed else "FAIL", "sun_mean": float(np.mean(s)),
        "off_mean": float(np.mean(o)), "difference": difference,
        "sun_off_ratio": float(np.mean(s) / np.mean(o)), "uncertainty": uncertainty,
        "snr": float(snr), "sun_repeatability_fraction": repeatability,
        "max_clipping_fraction": max_clip, "adc_margin": adc_margin,
        "broadband_fraction_positive": broadband_fraction_positive,
        "subband_fractional_difference": sub_ratio.tolist(),
    }


class DetectabilityCampaign:
    def __init__(self, args):
        self.args = args; self.output = Path(args.output_dir)
        config = json.loads(Path(args.observer_config).read_text())
        observer = config["observer"]
        from astropy.coordinates import EarthLocation
        self.location = EarthLocation(lat=observer["latitude_deg"] * u.deg,
                                      lon=observer["longitude_deg"] * u.deg,
                                      height=observer["elevation_m"] * u.m)
        sensors = config.get("temperature_sensors", {})
        self.temperatures = DS18B20Reader(sensors) if sensors else None
        self.telescope = self.sdr = None

    def targets(self):
        now = Time.now(); sun = sun_eod(now)
        candidates = []
        for east, north in ((12, 0), (-12, 0), (0, 12), (0, -12)):
            off = SkyCoord(lon=east * u.deg, lat=north * u.deg,
                           frame=SkyOffsetFrame(origin=sun)).transform_to(sun.frame)
            altitude = float(off.transform_to(AltAz(obstime=now, location=self.location)).alt.deg)
            if self.args.min_altitude <= altitude <= self.args.max_altitude:
                candidates.append((abs(altitude - 45), east, north, off, altitude))
        if not candidates:
            raise RuntimeError("no safe 12-degree OFF position")
        _, east, north, off, altitude = min(candidates)
        sun_alt = float(sun.transform_to(AltAz(obstime=now, location=self.location)).alt.deg)
        if not self.args.min_altitude <= sun_alt <= self.args.max_altitude:
            raise RuntimeError(f"Sun altitude {sun_alt:.1f} outside safety limits")
        return sun, off, {"east_deg": east, "north_deg": north,
                          "sun_altitude_deg": sun_alt, "off_altitude_deg": altitude}

    async def capture_one(self, index, gain, position_name, position, duration):
        now = Time.now(); altaz = position.transform_to(AltAz(obstime=now, location=self.location))
        path = self.output / f"gain_{gain:04.1f}_{index:02d}_{position_name.lower()}.h5"
        record = {"index": index, "gain_db": gain, "position": position_name,
                  "integration_seconds": duration, "commanded_ra_hours": float(position.ra.hour),
                  "commanded_dec_deg": float(position.dec.deg), "commanded_alt_deg": float(altaz.alt.deg),
                  "commanded_az_deg": float(altaz.az.deg), "hdf5_path": str(path), "status": "FAILED"}
        if not await self.telescope.goto(position.ra.hour, position.dec.deg):
            record["error"] = "GOTO failed"; return record
        await asyncio.sleep(self.args.settle)
        actual_ra, actual_dec = await self.telescope.get_coordinates(force_refresh=True)
        actual = SkyCoord(ra=actual_ra * u.hourangle, dec=actual_dec * u.deg, frame=position.frame)
        actual_aa = actual.transform_to(AltAz(obstime=Time.now(), location=self.location))
        record.update(actual_ra_hours=actual_ra, actual_dec_deg=actual_dec,
                      actual_alt_deg=float(actual_aa.alt.deg), actual_az_deg=float(actual_aa.az.deg),
                      temperatures_pre=self.temperatures.read_all() if self.temperatures else {},
                      capture_started_utc=datetime.now(timezone.utc).isoformat())
        await self.sdr.capture(duration, str(path), self.args.sample_rate,
                               {"gain": gain, "alignment_reference": "sun_detectability",
                                "position": position_name})
        record["capture_completed_utc"] = datetime.now(timezone.utc).isoformat()
        record["temperatures_post"] = self.temperatures.read_all() if self.temperatures else {}
        record.update(robust_broadband_metrics(path)); record["status"] = "VALID"
        print(f"GAIN {gain:4.1f} {position_name} power={record['broadband_power']:.6g} "
              f"clip={record['clipping_fraction']:.3%}", flush=True)
        return record

    async def run(self):
        self.output.mkdir(parents=True, exist_ok=False)
        sun, off, geometry = self.targets()
        self.telescope = INDITelescopeControl(self.args.host, self.args.port, self.args.device, self.args.verbose)
        self.sdr = SDRCapture("network", self.args.sdr_host, self.args.sdr_port, verbose=self.args.verbose)
        records = []
        try:
            if not await self.telescope.connect(): raise RuntimeError("INDI connection failed")
            await self.sdr.connect()
            index = 0
            for gain in self.args.gains:
                await self.sdr.configure(self.args.center_freq, self.args.sample_rate, gain=gain)
                for name in SEQUENCE:
                    index += 1
                    current_sun = sun_eod(Time.now())
                    if name == "SUN":
                        position = current_sun
                    else:
                        position = SkyCoord(lon=geometry["east_deg"] * u.deg,
                                            lat=geometry["north_deg"] * u.deg,
                                            frame=SkyOffsetFrame(origin=current_sun)).transform_to(current_sun.frame)
                    records.append(await self.capture_one(index, gain, name, position,
                                                          self.args.integration_seconds))
        finally:
            if self.sdr: await self.sdr.close()
            if self.telescope: await self.telescope.disconnect()
        summaries = {str(gain): summarize_gain([r for r in records if r["gain_db"] == gain])
                     for gain in self.args.gains}
        passing = [gain for gain in self.args.gains if summaries[str(gain)]["status"] == "PASS"]
        candidate = max(passing, key=lambda gain: summaries[str(gain)]["snr"]) if passing else None
        result = {"test": "ALMITA-ALIGNMENT-V2-SUN-HARDWARE-TEST-02-PHASE-A",
                  "timestamp_utc": datetime.now(timezone.utc).isoformat(), "geometry": geometry,
                  "sequence": list(SEQUENCE), "records": records, "gain_summary": summaries,
                  "candidate_gain_db": candidate,
                  "sun_detectability": "PASS" if candidate is not None else "FAIL",
                  "technical": "PASS" if len(records) == len(self.args.gains) * len(SEQUENCE) else "FAIL",
                  "pointing": "NOT TESTED", "sync": "NO", "next_authorized": "INTEGRATION_TEST" if candidate else "NONE"}
        (self.output / "sun_detectability_result.json").write_text(json.dumps(result, indent=2))
        with (self.output / "sun_detectability_records.csv").open("w", newline="") as handle:
            fields = [k for k, v in records[0].items() if not isinstance(v, (dict, list))]
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader(); writer.writerows(records)
        print(json.dumps({k: result[k] for k in ("technical", "sun_detectability", "candidate_gain_db", "next_authorized")}, indent=2))
        return 0 if candidate is not None else 2


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True); parser.add_argument("--observer-config", default="observer_config.json")
    parser.add_argument("--gains", type=float, nargs="+", default=list(GAINS)); parser.add_argument("--integration-seconds", type=float, default=1.0)
    parser.add_argument("--settle", type=float, default=2.0); parser.add_argument("--min-altitude", type=float, default=20.0)
    parser.add_argument("--max-altitude", type=float, default=80.0); parser.add_argument("--center-freq", type=int, default=1420405752)
    parser.add_argument("--sample-rate", type=int, default=2400000); parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624); parser.add_argument("--device", default="LX200 OnStep")
    parser.add_argument("--sdr-host", default="localhost"); parser.add_argument("--sdr-port", type=int, default=1234)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None): return asyncio.run(DetectabilityCampaign(parse_args(argv)).run())
if __name__ == "__main__": raise SystemExit(main())
