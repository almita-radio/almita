"""The dish's effective area from a LOBE solar transit and the Sun's measured flux at 1415 MHz.
(operator 2026-11-11: measure the lobe - "quizá una sorpresa positiva")

    python lobe_aeff.py data/lobe/<run> (--wizard data/calibration/<WIZARD session> | --tsys-k 350)
                        [--sun-flux-sfu 85 | --rstn-file FILE] [--geometric-area-m2 0.463] [--flux-uncertainty 0.10]

The Sun's flux: by default it is DOWNLOADED from NOAA SWPC (services.swpc.noaa.gov/json/solar-radio-flux.json: the
RSTN and other solar observatories' noon fluxes, ~30 days kept). Of the good-quality 1415 MHz values within
RSTN_WINDOW_H of the transit, the one measured CLOSEST IN TIME is used (operator 2026-11-11: "la de Chile" - Sagamore
Hill's noon is ~14 h Chile, when ALMITA scans the Sun; when it has no good value the next closest is taken). The
spread of all of them (stations differ by ~10-15 % on the same day - their own calibrations, the quiet Sun changes
over days) becomes the flux uncertainty when it is larger than --flux-uncertainty; their median is kept alongside. The downloaded file is kept in
<run>/aeff/ and reused when there is no internet (offline-first). --sun-flux-sfu overrides it by hand.

The Sun is a calibrated source. Solar radio observatories (the RSTN network: Learmonth, San Vito, Sagamore Hill,
Palehua) publish its flux density every day at 1415 MHz - 5 MHz from the HI line - in solar flux units
(1 sfu = 1e-22 W m^-2 Hz^-1). At 0.5 deg the Sun is a point for a ~20 deg beam. MAIN's dipole takes one linear
polarisation and the quiet Sun is unpolarised, so the antenna sees half of its flux:

    delta_T_A = A_eff x S / (2 k)          ->         A_eff = 2 k delta_T_A / S

LOBE measures delta_T_A as a ratio: the fitted peak over the background, times the background's temperature - the
system temperature, which the CALIBRATE wizard's Y-factor gives in kelvin (or --tsys-k). With a CROSS run (both cuts)
the peak of each cut is corrected for the pointing error along the other axis (a cut that passes beside the Sun sees
less than the peak), and the beam's solid angle Omega_MB gives a second, independent check:

    lambda^2 / Omega_A = A_eff,   Omega_A >= Omega_MB   ->   eta_MB = A_eff x Omega_MB / lambda^2  (must be <= 1)

An eta_MB above 1 means one of the inputs is off (most likely Tsys too high, or the flux of a different day/time).

Honest limits, written into the output:
  - the background is the sky beside the Sun in daytime, a little warmer than the cold sky of the Y-factor's Tsys,
    so delta_T_A - and A_eff - come out slightly LOW;
  - Tsys carries the wizard's T_rx assumption (its spread is carried through);
  - the published flux has its own uncertainty (--flux-uncertainty, assumed 10 % unless given) and is a daily value:
    check that no solar burst happened during the transit (the same RSTN record shows them);
  - the Gaussian fit assumes a Gaussian main beam.
Nothing in the LOBE run is modified: the result goes to <run>/aeff/lobe_aeff.json.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

K_BOLTZMANN = 1.380649e-23
SFU = 1e-22                                   # W m^-2 Hz^-1
HI_REST_HZ = 1420405751.768
RSTN_URL = "https://services.swpc.noaa.gov/json/solar-radio-flux.json"
RSTN_FREQ_MHZ = 1415
RSTN_WINDOW_H = 18.0
# the reflector: 90 x 60 cm, long sides straight, short ends rounded (operator 2026-11-11). With semicircular ends
# (radius 30 cm) the outline is 0.30 x 0.60 + pi 0.30^2 = 0.463 m^2; a plain rectangle would be 0.54 m^2.
DEFAULT_GEOMETRIC_AREA_M2 = 0.463
WAVELENGTH_M = 299792458.0 / HI_REST_HZ       # 0.2111 m


def load_tsys(wizard_dir: Path) -> Dict[str, float]:
    """Cold-sky Tsys (and its T_rx spread) from a CALIBRATE wizard's VALID Y-factor; refuses anything else."""
    state = json.loads((wizard_dir / "wizard_state.json").read_text())
    y = state.get("y_factor")
    if not y:
        raise ValueError(f"{wizard_dir}: no Y-factor in wizard_state.json")
    if y.get("verdict") != "VALID":
        raise ValueError(f"{wizard_dir}: Y-factor not VALID ({y.get('verdict')}): {y.get('reason')}")
    spread = [s["tsys_sky_k"] for s in y.get("spread_for_t_rx") or []] or [y["tsys_sky_k"]]
    return {"tsys_k": float(y["tsys_sky_k"]), "tsys_low_k": float(min(spread)), "tsys_high_k": float(max(spread)),
            "source": f"Y-factor {y.get('y_db')} dB ({wizard_dir.name})"}


def transit_mid_utc(result: Dict[str, Any]):
    """When the Sun was in the beam: the plan's start + half its duration."""
    from datetime import datetime, timedelta
    p = result.get("plan") or {}
    t0 = datetime.fromisoformat(str(p["computed_utc"]).replace("Z", "+00:00"))
    return t0 + timedelta(seconds=float(p.get("duration_s") or 0) / 2)


def download_rstn(dest: Path, timeout_s: float = 20.0) -> Path:
    """Fetch NOAA SWPC's solar radio flux list into dest (kept for provenance and offline reruns)."""
    import urllib.request
    req = urllib.request.Request(RSTN_URL, headers={"User-Agent": "almita-lobe-aeff"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        body = r.read()
    json.loads(body)                                  # refuse to keep something that is not JSON
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


def flux_from_rstn(records, when, window_h: float = RSTN_WINDOW_H, freq_mhz: int = RSTN_FREQ_MHZ) -> Dict[str, Any]:
    """The good-quality values at freq_mhz within window_h of `when` (aware UTC datetime): median and spread."""
    from datetime import datetime, timezone
    used = []
    for r in records:
        try:
            t = datetime.fromisoformat(str(r["time_tag"]).replace("Z", "")).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        dt_h = (t - when).total_seconds() / 3600.0
        if abs(dt_h) > window_h:
            continue
        for d in r.get("details") or []:
            if d.get("frequency") == freq_mhz and d.get("flux") not in (None, -1) and str(d.get("observed_quality", "")).startswith("Good"):
                used.append({"station": r.get("common_name"), "time_utc": r["time_tag"], "hours_from_transit": round(dt_h, 2),
                             "flux_sfu": float(d["flux"])})
    seen, uniq = set(), []
    for u in used:                                    # the feed repeats a station's report a few minutes apart
        key = (u["station"], u["time_utc"][:10], u["flux_sfu"])
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    if not uniq:
        raise ValueError(f"no good-quality {freq_mhz} MHz solar flux within {window_h:g} h of the transit ({when.isoformat()}) - "
                         "give it by hand with --sun-flux-sfu")
    fl = sorted(u["flux_sfu"] for u in uniq)
    med = float(np.median(fl))
    near = min(uniq, key=lambda u: abs(u["hours_from_transit"]))
    return {"flux_sfu": near["flux_sfu"], "chosen": near, "rule": "closest in time to the transit", "median_sfu": med,
            "values": uniq, "spread_fraction": (fl[-1] - fl[0]) / 2 / near["flux_sfu"] if len(fl) > 1 else None,
            "window_h": window_h, "frequency_mhz": freq_mhz, "source": RSTN_URL}


def rstn_flux(lobe_dir: Path, result: Dict[str, Any], rstn_file: Optional[Path] = None, download=download_rstn) -> Dict[str, Any]:
    """The flux for this transit: a given file, else a fresh download, else the copy kept from an earlier run."""
    kept = lobe_dir / "aeff" / "solar_radio_flux.json"
    note = None
    if rstn_file is None:
        try:
            rstn_file = download(kept)
            note = "downloaded"
        except Exception as exc:  # noqa: BLE001 - offline-first: fall back to the kept copy
            if not kept.is_file():
                raise ValueError(f"could not download the solar flux ({exc}) and no copy is kept - give --sun-flux-sfu") from exc
            rstn_file, note = kept, f"offline: kept copy from an earlier run ({exc.__class__.__name__})"
    out = flux_from_rstn(json.loads(Path(rstn_file).read_text()), transit_mid_utc(result))
    out.update(file=str(rstn_file), fetched=note or "given file")
    return out


def cross_axis_loss(x0_other_deg: Optional[float], fwhm_other_deg: Optional[float]) -> float:
    """Fraction of the peak a cut sees when it passes x0 beside the Sun along the other axis (Gaussian beam)."""
    if x0_other_deg is None or not fwhm_other_deg:
        return 1.0
    return math.exp(-4 * math.log(2) * x0_other_deg ** 2 / fwhm_other_deg ** 2)


def peak_ratio(result: Dict[str, Any]) -> Dict[str, Any]:
    """The Sun's peak over the background from lobe_result.json: one cut, or both cuts each corrected for the pointing
    error along the other axis and averaged."""
    fr, fd, beam = result.get("fit") or {}, result.get("fit_dec") or {}, result.get("beam")
    if beam:
        r_ra = fr["peak_over_background"] / cross_axis_loss(beam["peak_offset_dec_deg"], beam["fwhm_dec_deg"])
        r_dec = fd["peak_over_background"] / cross_axis_loss(beam["peak_offset_ra_deg"], beam["fwhm_ra_deg"])
        return {"ratio": (r_ra + r_dec) / 2, "ratio_ra_corrected": r_ra, "ratio_dec_corrected": r_dec,
                "spread_between_cuts": abs(r_ra - r_dec) / ((r_ra + r_dec) / 2), "cuts": 2}
    f = fr if fr.get("peak_over_background") else fd
    if f.get("status") != "FIT" or not f.get("peak_over_background"):
        raise ValueError(f"no clean transit fit in lobe_result.json (status {f.get('status')}, flags {f.get('flags')})")
    return {"ratio": float(f["peak_over_background"]), "cuts": 1,
            "note": "one cut only: a pointing error across it would make the peak (and A_eff) LOW - a CROSS run corrects that"}


def effective_area(ratio: float, tsys_k: float, flux_sfu: float) -> float:
    return 2 * K_BOLTZMANN * ratio * tsys_k / (flux_sfu * SFU)


def analyse(lobe_dir: Path, flux_sfu: Optional[float], tsys: Dict[str, Any], geometric_area_m2: Optional[float] = DEFAULT_GEOMETRIC_AREA_M2,
            flux_uncertainty: float = 0.10, rstn_file: Optional[Path] = None, download=download_rstn) -> Dict[str, Any]:
    res = json.loads((lobe_dir / "lobe_result.json").read_text())
    flux_info: Dict[str, Any] = {"flux_sfu": flux_sfu, "source": "given by hand"}
    if flux_sfu is None:
        flux_info = rstn_flux(lobe_dir, res, rstn_file, download)
        flux_sfu = flux_info["flux_sfu"]
        if flux_info.get("spread_fraction"):
            flux_uncertainty = max(flux_uncertainty, flux_info["spread_fraction"])
    if not (1 <= flux_sfu <= 10000):
        raise ValueError("--sun-flux-sfu must be 1..10000 sfu")
    pr = peak_ratio(res)
    a = effective_area(pr["ratio"], tsys["tsys_k"], flux_sfu)
    lo = effective_area(pr["ratio"], tsys["tsys_low_k"], flux_sfu * (1 + flux_uncertainty))
    hi = effective_area(pr["ratio"], tsys["tsys_high_k"], flux_sfu * (1 - flux_uncertainty))
    out: Dict[str, Any] = {
        "schema": "almita.lobe_aeff.v1", "lobe_run": str(lobe_dir), "sun_flux_sfu": flux_sfu, "flux_uncertainty": flux_uncertainty,
        "sun_flux": flux_info,
        "tsys": tsys, "peak": pr, "delta_t_a_k": pr["ratio"] * tsys["tsys_k"],
        "effective_area_m2": a, "effective_area_range_m2": [lo, hi], "wavelength_m": WAVELENGTH_M,
        "limits": ["the daytime background beside the Sun is warmer than the cold-sky Tsys used: delta_T_A and A_eff slightly LOW",
                   "Tsys carries the wizard's T_rx assumption (range carried through)",
                   "the flux is a published daily value: check for solar bursts during the transit"],
    }
    if geometric_area_m2:
        out.update(geometric_area_m2=geometric_area_m2, aperture_efficiency=a / geometric_area_m2,
                   aperture_efficiency_range=[lo / geometric_area_m2, hi / geometric_area_m2])
    beam = res.get("beam")
    if beam:
        eta_mb = a * beam["solid_angle_sr"] / WAVELENGTH_M ** 2
        out.update(beam=beam, max_effective_area_from_beam_m2=WAVELENGTH_M ** 2 / beam["solid_angle_sr"], main_beam_efficiency=eta_mb)
        if eta_mb > 1.0:
            out["warning"] = (f"main-beam efficiency {eta_mb:.2f} > 1 is impossible: an input is off (Tsys too high? flux of another "
                              "day? a burst?)")
    out_dir = lobe_dir / "aeff"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "lobe_aeff.json").write_text(json.dumps(out, indent=2))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lobe_run")
    ap.add_argument("--sun-flux-sfu", type=float, default=None, help="the Sun's 1415 MHz flux by hand (sfu); default: downloaded from NOAA SWPC")
    ap.add_argument("--rstn-file", default=None, help="a saved copy of NOAA SWPC's solar-radio-flux.json to use instead of downloading")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--wizard", help="CALIBRATE wizard session with a VALID Y-factor")
    g.add_argument("--tsys-k", type=float, help="system temperature in K, given by hand")
    ap.add_argument("--geometric-area-m2", type=float, default=DEFAULT_GEOMETRIC_AREA_M2,
                    help="the reflector's area (default: 90 x 60 cm with rounded short ends, 0.463 m^2)")
    ap.add_argument("--flux-uncertainty", type=float, default=0.10)
    a = ap.parse_args(argv)
    try:
        tsys = load_tsys(Path(a.wizard)) if a.wizard else {"tsys_k": a.tsys_k, "tsys_low_k": a.tsys_k, "tsys_high_k": a.tsys_k,
                                                            "source": "given by hand"}
        r = analyse(Path(a.lobe_run), a.sun_flux_sfu, tsys, a.geometric_area_m2, a.flux_uncertainty,
                    Path(a.rstn_file) if a.rstn_file else None)
    except (ValueError, OSError, KeyError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    sf = r["sun_flux"]
    if sf.get("values"):
        c = sf["chosen"]
        print(f"Sun flux {r['sun_flux_sfu']:.0f} sfu at {sf['frequency_mhz']} MHz: {c['station']}, {c['hours_from_transit']:+.1f} h from the transit "
              f"(closest); all within {sf['window_h']:g} h: " +
              ", ".join(f"{v['station']} {v['flux_sfu']:.0f}" for v in sf["values"]) + f", median {sf['median_sfu']:.1f} [{sf['fetched']}]")
    print(f"Sun peak / background {r['peak']['ratio']:.4f} ({r['peak']['cuts']} cut(s)) x Tsys {r['tsys']['tsys_k']:.0f} K "
          f"= delta T_A {r['delta_t_a_k']:.1f} K")
    print(f"effective area {r['effective_area_m2']:.3f} m^2 [{r['effective_area_range_m2'][0]:.3f}-{r['effective_area_range_m2'][1]:.3f}]")
    if "aperture_efficiency" in r:
        print(f"aperture efficiency {r['aperture_efficiency']:.0%} of {r['geometric_area_m2']:.3f} m^2")
    if "main_beam_efficiency" in r:
        print(f"beam {r['beam']['fwhm_ra_deg']:.1f} x {r['beam']['fwhm_dec_deg']:.1f} deg -> main-beam efficiency {r['main_beam_efficiency']:.2f}")
    if r.get("warning"):
        print("WARNING: " + r["warning"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
