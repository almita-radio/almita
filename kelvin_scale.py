"""From relative spectra to kelvin: antenna temperature from the CALIBRATE wizard's Y-factor, and - where HI4PI covers
the beam - an estimate of the antenna's efficiency against HI4PI's brightness temperature.
(operator 2026-11-10: "¿vamos a poder medir en Kelvin la próxima vez que nos subamos?")

    python kelvin_scale.py data/reduced/<campaign>/<REDUCE session> data/calibration/<WIZARD session> [--out DIR]

REDUCE's spectra are the fractional excess over the system's own level: T_line / Tsys. The wizard's Y-factor (50 ohm
load at the LNA input vs the cold sky HI_BAJO, calibration_engine.y_factor) gives Tsys on cold sky. So

    T_A(v) ~ relative_intensity(v) x Tsys_cold        (antenna temperature at the LNA input, K)

Honest limits, written into the output:
  - Tsys at a pointing on the Milky Way is a little higher than on the cold sky (continuum, ground at low elevation):
    the kelvin figures here are slightly LOW, by roughly that difference (~5-20 K of a few hundred);
  - Tsys depends on the receiver temperature T_rx, a datasheet ASSUMPTION in the wizard (35-80 K spread carried
    through as the low/high columns);
  - T_A is not HI4PI's brightness temperature: the antenna loses part of the signal (spill-over, mismatch, cable).
    Where an HI4PI tile holds the beam, eta = (integral of T_A) / (integral of HI4PI through the same beam) is
    reported per point and as a median - an efficiency ESTIMATE, not a calibration applied to anything.

It refuses (and says why) when the Y-factor is missing or not VALID. Nothing in REDUCE's session is modified: every
output goes to <REDUCE session>/kelvin (or --out).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

import rotation_curve as rc


def load_y_factor(wizard_dir: Path) -> Dict[str, Any]:
    state = json.loads((wizard_dir / "wizard_state.json").read_text())
    y = state.get("y_factor")
    if not y:
        raise ValueError(f"{wizard_dir}: no Y-factor in wizard_state.json - run "
                         f"`python calibrate_reference_wizard.py y-factor --session-dir {wizard_dir}` first")
    if y.get("verdict") != "VALID":
        raise ValueError(f"{wizard_dir}: Y-factor not VALID ({y.get('verdict')}): {y.get('reason')}")
    return y


def tsys_range(y: Dict[str, Any]) -> Dict[str, float]:
    spread = [s["tsys_sky_k"] for s in y.get("spread_for_t_rx") or []] or [y["tsys_sky_k"]]
    return {"tsys_k": float(y["tsys_sky_k"]), "tsys_low_k": float(min(spread)), "tsys_high_k": float(max(spread)),
            "t_rx_assumption": y.get("t_rx_assumption"), "y_db": y.get("y_db"), "t_load_k": y.get("t_load_k")}


def scale(reduce_dir: Path, wizard_dir: Path, out: Optional[Path] = None, *, hi4pi: Optional[rc.Hi4piTiles] = None,
          beam_fwhm_deg: float = 20.0, window_km_s=(-100.0, 100.0)) -> Dict[str, Any]:
    y = load_y_factor(wizard_dir)
    ts = tsys_range(y)
    out = out or (reduce_dir / "kelvin")
    (out / "points").mkdir(parents=True, exist_ok=True)
    hi4pi = hi4pi if hi4pi is not None else rc.Hi4piTiles()
    rows: List[Dict[str, Any]] = []
    etas = []
    for p in rc.load_points(reduce_dir):
        v, rel = p["v"], p["t"]
        ta = rel * ts["tsys_k"]
        ok = np.isfinite(ta) & (v >= window_km_s[0]) & (v <= window_km_s[1])
        dv = float(np.median(np.abs(np.diff(v)))) if v.size > 1 else 0.0
        row: Dict[str, Any] = {"point_index": p["point_index"], "ra_hours": p["ra_hours"], "dec_degrees": p["dec_degrees"]}
        l, b = rc.galactic(p["ra_hours"], p["dec_degrees"])
        row.update(l_deg=round(l, 2), b_deg=round(b, 2))
        if ok.sum() > 10:
            sm = rc.smooth(v, ta)
            k = int(np.nanargmax(np.where(ok, sm, np.nan)))
            row.update(peak_ta_k=round(float(sm[k]), 2), peak_v_km_s=round(float(v[k]), 1),
                       peak_ta_k_range=[round(float(sm[k] / ts["tsys_k"] * ts["tsys_low_k"]), 2),
                                        round(float(sm[k] / ts["tsys_k"] * ts["tsys_high_k"]), 2)],
                       integral_k_km_s=round(float(np.nansum(ta[ok]) * dv), 1))
            ref = hi4pi.beam_spectrum(l, b, beam_fwhm_deg) if hi4pi.tiles else None
            if ref is not None:
                hv, hs, cover = ref
                hm = (hv >= window_km_s[0]) & (hv <= window_km_s[1])
                tb_int = float(np.nansum(hs[hm]) * float(np.median(np.diff(hv))))
                if tb_int > 0:
                    eta = row["integral_k_km_s"] / tb_int
                    row["hi4pi"] = {"integral_k_km_s": round(tb_int, 1), "eta_estimate": round(eta, 3),
                                    "beam_cover": round(cover, 3)}
                    etas.append(eta)
        (out / "points" / f"{p['point_index']}.json").write_text(json.dumps({
            "point_index": p["point_index"], "velocity_lsrk_km_s": [round(float(x), 3) for x in v],
            "antenna_temperature_k": [None if not np.isfinite(x) else round(float(x), 4) for x in ta]}))
        rows.append(row)
    result = {
        "schema": "almita.kelvin_scale.v1", "reduce_session": str(reduce_dir), "wizard_session": str(wizard_dir),
        "tsys": ts, "window_km_s": list(window_km_s), "beam_fwhm_deg": beam_fwhm_deg,
        "points": rows, "eta_median": round(float(np.median(etas)), 3) if etas else None, "eta_points": len(etas),
        "what": "T_A = relative_intensity x Tsys(cold sky, Y-factor) - antenna temperature at the LNA input",
        "limits": ["Tsys on the Milky Way is a little above the cold-sky Tsys used here: T_A slightly LOW",
                   "T_rx is a datasheet assumption: see tsys_low_k / tsys_high_k and each point's peak_ta_k_range",
                   "eta compares integrals over the window with HI4PI through the same Gaussian beam: an estimate, "
                   "never applied; HI4PI's all-sky survey is the reference, not ALMITA"],
    }
    (out / "kelvin_scale.json").write_text(json.dumps(result, indent=2, default=float))
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reduce_session")
    ap.add_argument("wizard_session")
    ap.add_argument("--out", default=None)
    ap.add_argument("--vmin", type=float, default=-100.0)
    ap.add_argument("--vmax", type=float, default=100.0)
    a = ap.parse_args(argv)
    try:
        r = scale(Path(a.reduce_session), Path(a.wizard_session), Path(a.out) if a.out else None,
                  window_km_s=(a.vmin, a.vmax))
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 2
    t = r["tsys"]
    print(f"Tsys (cold sky) {t['tsys_k']:.0f} K [{t['tsys_low_k']:.0f}-{t['tsys_high_k']:.0f} K for the T_rx spread], Y {t['y_db']} dB")
    for p in r["points"]:
        if "peak_ta_k" in p:
            e = f"  eta ~{p['hi4pi']['eta_estimate']:.2f}" if "hi4pi" in p else ""
            print(f"  point {p['point_index']:3d}  l={p['l_deg']:6.1f} b={p['b_deg']:+5.1f}  peak T_A {p['peak_ta_k']:6.1f} K "
                  f"at {p['peak_v_km_s']:+6.1f} km/s{e}")
    print(f"median eta {r['eta_median']} over {r['eta_points']} point(s) with HI4PI coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
