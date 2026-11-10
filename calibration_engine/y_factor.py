"""Receiver Y-factor from the CALIBRATE wizard's own captures (operator 2026-11-10: "el Y-factor tenemos que tenerlo
calculado desde siempre, tiene que estar dentro del cálculo que hace la calibración").

    Y = P(50 ohm load) / P(cold sky)

P is the MEDIAN power spectral density over the inner 80 % of the band, DC excluded and averaged over the step's
captures. A median ignores the narrow RFI lines that a total-power ratio would add in, and the HI line itself
(~1 % of the band).

With the load at the LNA INPUT (the normal wizard connection point) it replaces the antenna exactly, so
    P_load ~ T_load + T_rx        P_sky ~ T_ant + T_rx
    Tsys(sky) = T_ant + T_rx = (T_load + T_rx) / Y
    T_ant (at the LNA input) = Tsys(sky) - T_rx
T_load comes from the DS18 on the load (observer_config temperature_sensors role "load"), or is declared by the
operator when no reading exists. T_rx is the LNA's datasheet figure, declared as an ASSUMPTION
(Nooelec SAWbird+ H1, NF ~0.8 dB -> ~59 K); the result also gives the spread for 35-80 K.

The cold-sky reference is HI_BAJO: the sky zone with the weakest HI4PI line, by construction of the wizard.

What this is NOT: an absolute flux calibration. calibration_level.CURRENT_LEVEL stays OPERATIONAL_RELATIVE and
REDUCE applies none of these numbers. It is a figure of merit of the receiver plus antenna chain, meant to be
compared night to night and with the model (feed v1: Y ~5.5 dB).

Validity checks (each listed in "checks", the verdict is the worst one):
- the same rtl_tcp process (MainPID and ActiveEnterTimestamp) for the load and the sky: the gain jumps ~1.7 dB
  across an rtl_tcp restart (2026-10-18), which would read as a false Y;
- the same requested center frequency, sample rate and gain on both;
- neither step clipped;
- Y > 1 (the load at ambient must be hotter than a cold sky); Y <= 1 means RFI, a pointing problem or a broken
  chain;
- any sky reference (HI_ALTO too) brighter than the ambient load is flagged: the sky cannot do that at 21 cm,
  only interference can.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

FFT_SIZE = 4096
INNER_BAND_FRACTION = 0.80      # use the central 80 % of the band (the RTL's anti-alias filter rolls off at the edges)
DC_HALF_WIDTH_BINS = 20         # +-20 bins (+-11.7 kHz at 2.4 MS/s) around the RTL's DC spike
T_RX_ASSUMED_K = 59.0           # SAWbird+ H1 datasheet NF ~0.8 dB - an assumption, not a measurement
T_RX_SPREAD_K = (35.0, 80.0)
KELVIN_OFFSET = 273.15

VALID = "VALID"                          # Y + temperatures: Tsys / T_ant computed
Y_ONLY = "Y_ONLY"                        # Y is good, but no load temperature: no kelvin figures
INVALID = "INVALID"                      # a check failed: the number must not be used


def _psd_from_uint8(iq: np.ndarray, fft_size: int = FFT_SIZE) -> np.ndarray:
    x = np.asarray(iq, dtype=np.float32) - 127.5
    z = x[0::2] + 1j * x[1::2]
    n_frames = len(z) // fft_size
    if n_frames < 1:
        raise ValueError(f"capture too short for a {fft_size}-point spectrum")
    frames = z[:n_frames * fft_size].reshape(n_frames, fft_size) * np.hanning(fft_size)
    spectrum = np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)
    return (np.abs(spectrum) ** 2).mean(axis=0)


def band_channels(fft_size: int = FFT_SIZE) -> np.ndarray:
    lo = int(round(fft_size * (1 - INNER_BAND_FRACTION) / 2))
    hi = fft_size - lo
    centre = fft_size // 2
    return np.r_[lo:centre - DC_HALF_WIDTH_BINS, centre + DC_HALF_WIDTH_BINS + 1:hi]


def mean_psd(iq_arrays: Iterable[np.ndarray], fft_size: int = FFT_SIZE) -> np.ndarray:
    spectra = [_psd_from_uint8(iq, fft_size) for iq in iq_arrays]
    if not spectra:
        raise ValueError("no captures")
    return np.mean(spectra, axis=0)


def band_power(psd: np.ndarray) -> float:
    return float(np.median(psd[band_channels(len(psd))]))


def _db(x: float) -> float:
    return 10.0 * math.log10(x)


def load_temperature_record(pre: Optional[Dict[str, Any]], post: Optional[Dict[str, Any]],
                            sensor_id: Optional[str]) -> Dict[str, Any]:
    """The DS18 'load' readings around the 50 ohm captures -> one record. Never invents a value: no valid reading
    gives temperature_c None and the reason."""
    values = [r["temperature_c"] for r in (pre, post) if r and r.get("valid") and r.get("temperature_c") is not None]
    errors = [r.get("error") for r in (pre, post) if r and not r.get("valid")]
    record: Dict[str, Any] = {"sensor_id": sensor_id, "pre": pre, "post": post,
                              "temperature_c": (float(np.mean(values)) if values else None),
                              "drift_c": (float(values[-1] - values[0]) if len(values) == 2 else None),
                              "source": ("DS18 load sensor" if values else None)}
    if not values:
        record["reason"] = ("no 'load' sensor in observer_config temperature_sensors" if sensor_id is None
                            else f"DS18 'load' gave no valid reading: {'; '.join(e for e in errors if e) or 'unknown'}")
    return record


def _same_process_check(load_proc: Optional[Dict[str, Any]], sky_proc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not load_proc or not sky_proc or load_proc.get("main_pid") in (None, 0) or sky_proc.get("main_pid") in (None, 0):
        return {"name": "same_rtl_tcp_process", "status": "UNVERIFIED",
                "detail": "rtl_tcp process identity was not recorded for both steps (sessions before 2026-11-10 never "
                          "recorded it) - a restart in between cannot be ruled out"}
    same = (load_proc.get("main_pid") == sky_proc.get("main_pid")
            and load_proc.get("active_enter") == sky_proc.get("active_enter"))
    return {"name": "same_rtl_tcp_process", "status": "OK" if same else "FAIL",
            "detail": (f"MainPID {load_proc.get('main_pid')} since {load_proc.get('active_enter')} for both" if same else
                       f"load: MainPID {load_proc.get('main_pid')} since {load_proc.get('active_enter')}; sky: MainPID "
                       f"{sky_proc.get('main_pid')} since {sky_proc.get('active_enter')} - rtl_tcp was restarted in "
                       "between (the gain jumps ~1.7 dB on a restart), so Y is not valid")}


def compute_y_factor(load_iq: Sequence[np.ndarray], cold_iq: Sequence[np.ndarray], *,
                     connection_point: str,
                     load_temperature_c: Optional[float], load_temperature_source: Optional[str],
                     load_tuning: Optional[Dict[str, Any]] = None, cold_tuning: Optional[Dict[str, Any]] = None,
                     load_process: Optional[Dict[str, Any]] = None, cold_process: Optional[Dict[str, Any]] = None,
                     load_clipped: bool = False, cold_clipped: bool = False,
                     other_sky: Optional[Dict[str, Sequence[np.ndarray]]] = None,
                     t_rx_k: float = T_RX_ASSUMED_K) -> Dict[str, Any]:
    p_load_psd, p_cold_psd = mean_psd(load_iq), mean_psd(cold_iq)
    p_load, p_cold = band_power(p_load_psd), band_power(p_cold_psd)
    y = p_load / p_cold if p_cold > 0 else float("nan")
    ratio = p_load_psd[band_channels(len(p_load_psd))] / p_cold_psd[band_channels(len(p_cold_psd))]
    checks: List[Dict[str, Any]] = [_same_process_check(load_process, cold_process)]

    keys = ("center_frequency_hz", "sample_rate_hz", "gain_db")
    req_l = (load_tuning or {}).get("requested") or {}
    req_c = (cold_tuning or {}).get("requested") or {}
    if req_l and req_c:
        diff = [k for k in keys if req_l.get(k) != req_c.get(k)]
        checks.append({"name": "same_tuning", "status": "FAIL" if diff else "OK",
                       "detail": (f"differs in {', '.join(diff)}: load {req_l} vs sky {req_c}" if diff
                                  else f"{req_l.get('center_frequency_hz')} Hz, {req_l.get('sample_rate_hz')} S/s, "
                                       f"gain {req_l.get('gain_db')} dB on both")})
    else:
        checks.append({"name": "same_tuning", "status": "UNVERIFIED", "detail": "tuning record missing for one step"})
    checks.append({"name": "no_clipping", "status": "FAIL" if (load_clipped or cold_clipped) else "OK",
                   "detail": ("clipped: " + ", ".join(n for n, c in (("load", load_clipped), ("cold sky", cold_clipped)) if c))
                   if (load_clipped or cold_clipped) else "neither step clipped"})
    checks.append({"name": "load_hotter_than_cold_sky", "status": "OK" if y > 1.0 else "FAIL",
                   "detail": (f"Y = {y:.3f}" if y > 1.0 else
                              f"Y = {y:.3f} <= 1: the cold sky read as hot as or hotter than an ambient load - RFI, "
                              "pointing at the ground/Sun or a broken chain; the number is not a receiver figure")})
    warnings: List[str] = []
    sky_vs_load: Dict[str, float] = {}
    for label, arrays in (other_sky or {}).items():
        if not arrays:
            continue
        p = band_power(mean_psd(arrays))
        sky_vs_load[label] = round(_db(p / p_load), 2)
        if p > p_load:
            warnings.append(f"{label} read {_db(p / p_load):+.1f} dB ABOVE the ambient 50 ohm load - the sky cannot "
                            "be hotter than ~290 K at 21 cm away from the Sun, so that pointing had interference "
                            "(or saw the ground/Sun); treat that reference with suspicion")

    result: Dict[str, Any] = {
        "method": f"median PSD over the inner {INNER_BAND_FRACTION:.0%} of the band, DC +-{DC_HALF_WIDTH_BINS} bins "
                  f"excluded, {FFT_SIZE}-point FFT, averaged over the step's captures",
        "load_reference": "AMBIENT_50R", "cold_reference": "HI_BAJO", "connection_point": connection_point,
        "p_load": p_load, "p_cold": p_cold,
        "y_linear": round(y, 4), "y_db": round(_db(y), 2) if y > 0 else None,
        "y_db_per_channel_p10_p50_p90": [round(_db(v), 2) for v in np.percentile(ratio, [10, 50, 90])],
        "sky_minus_load_db": sky_vs_load,
        "load_temperature_c": load_temperature_c, "load_temperature_source": load_temperature_source,
        "checks": checks, "warnings": warnings,
        "calibration_level": "OPERATIONAL_RELATIVE",
        "not_applied": "a receiver figure of merit - REDUCE applies none of these numbers (no kelvin conversion)",
    }
    failed = [c for c in checks if c["status"] == "FAIL"]
    if failed:
        result.update(verdict=INVALID, reason="; ".join(f"{c['name']}: {c['detail']}" for c in failed))
        return result
    if connection_point != "LNA_INPUT":
        result.update(verdict=Y_ONLY, reason=f"the load was at {connection_point}, not in place of the antenna at the "
                                             "LNA input - Y compares different reference planes, no Tsys/T_ant")
        return result
    if load_temperature_c is None:
        result.update(verdict=Y_ONLY, reason="no load temperature (DS18 'load' unread and none declared) - Y only, "
                                             "no kelvin figures")
        return result
    t_load_k = load_temperature_c + KELVIN_OFFSET

    def kelvin(t_rx: float) -> Dict[str, float]:
        tsys = (t_load_k + t_rx) / y
        return {"t_rx_k": t_rx, "tsys_sky_k": round(tsys, 1), "t_ant_at_lna_input_k": round(tsys - t_rx, 1)}

    best = kelvin(t_rx_k)
    result.update(verdict=VALID, reason=f"Y = {y:.3f} ({_db(y):.2f} dB) with T_load {t_load_k:.1f} K",
                  t_load_k=round(t_load_k, 2), **best,
                  t_rx_assumption=f"T_rx {t_rx_k:g} K is the LNA datasheet figure (SAWbird+ H1, NF ~0.8 dB), NOT measured",
                  spread_for_t_rx=[kelvin(t) for t in T_RX_SPREAD_K])
    if any(c["status"] == "UNVERIFIED" for c in checks):
        result["reason"] += " - " + "; ".join(c["detail"] for c in checks if c["status"] == "UNVERIFIED")
    return result
