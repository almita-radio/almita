#!/usr/bin/env python3
"""ALMITA SCIENCE FEATURE FORENSICS CLI (additive to the frozen SCIENCE V1).

Diagnoses whether an operator-specified spectral feature follows the sky, time / capture order, the receiver or RFI. Consumes
only a REDUCE Level 1 session (and optionally reads a SCIENCE session); writes only to its own output root; never opens RAW,
never touches hardware or the network. See docs/SCIENCE_FORENSICS_RUNBOOK.md.

  inspect  REDUCE_DIR [--scan-v-min M/S --scan-v-max M/S]     read-only summary; optionally locate the strongest excursion per point
  plan     REDUCE_DIR --center X --half-width Y [...]          no writes: what would run and why it could be blocked
  run      REDUCE_DIR --center X --half-width Y [...]          write a FORENSICS session
  validate FORENSICS_SESSION_DIR                                read-only integrity check
  compare  SESSION_A SESSION_B                                  read-only numeric comparison
"""
from __future__ import annotations

import argparse
import json
import sys


def _emit(payload, as_json, lines):
    if as_json:
        from science_forensics.models import sanitize
        print(json.dumps(sanitize(payload), indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _config(args):
    from science_forensics.ingest import site_from_observer_config
    from science_forensics.models import ForensicsConfig
    kw = dict(window_center=args.center, window_half_width=args.half_width, window_frame=args.window_frame, sign=args.sign,
              centroid_method=args.method, feature_id=args.feature_id)
    if args.integration_window_min_m_s is not None:
        kw["integration_window_min_m_s"] = args.integration_window_min_m_s
    if args.integration_window_max_m_s is not None:
        kw["integration_window_max_m_s"] = args.integration_window_max_m_s
    if args.site_from_observer_config is not None:
        kw.update(site_from_observer_config(args.site_from_observer_config))
    if args.sort_modes:
        kw["sort_modes"] = tuple(args.sort_modes.split(","))
    return ForensicsConfig(**kw)


def cmd_inspect(args) -> int:
    import numpy as np
    from science_forensics.ingest import load_forensics_input
    from science_forensics.measure import scan_peaks
    inp = load_forensics_input(args.reduce_session_dir)
    t = np.array([p.t_mid_s for p in inp.points])
    payload = {"campaign_id": inp.campaign_id, "reduce_session_id": inp.reduce_session_id, "status": inp.reduce_session_status,
               "n_points": len(inp.points), "n_excluded": len(inp.exclusions), "time_span_minutes": float((t.max() - t.min()) / 60),
               "ra_deg_range": [min(p.ra_deg for p in inp.points), max(p.ra_deg for p in inp.points)],
               "dec_deg_range": [min(p.dec_deg for p in inp.points), max(p.dec_deg for p in inp.points)],
               "rest_frequency_hz": inp.rest_frequency_hz,
               "lsrk_shift_range_m_s": [min(p.lsrk_shift_m_s for p in inp.points), max(p.lsrk_shift_m_s for p in inp.points)],
               "metadata_unavailable_in_level1": inp.unavailable}
    lines = [f"campaign {inp.campaign_id}  REDUCE {inp.reduce_session_id} ({inp.reduce_session_status})",
             f"points {payload['n_points']} (excluded {payload['n_excluded']}), span {payload['time_span_minutes']:.1f} min, "
             f"LSRK shift range {payload['lsrk_shift_range_m_s'][0]:.0f}..{payload['lsrk_shift_range_m_s'][1]:.0f} m/s"]
    if args.scan_v_min is not None and args.scan_v_max is not None:
        rows = scan_peaks(inp.points, args.scan_v_min, args.scan_v_max)
        ok = [r for r in rows if r.get("status") == "OK"]
        payload["scan"] = rows
        if ok:
            v = np.array([r["velocity_lsrk_m_s"] for r in ok])
            sug_c, sug_h = float(0.5 * (v.min() + v.max())), float(0.5 * (v.max() - v.min()) + 3000.0)
            payload["suggested_window_lsrk_m_s"] = {"center": sug_c, "half_width": sug_h,
                                                    "basis": "per-point strongest smoothed excursion in the scanned range +/- 3 km/s"}
            lines.append("strongest excursion per point (LSRK m/s, topocentric Hz, smoothed excursion, sigma):")
            lines += [f"  point {r['point_index']:>4}: {r['velocity_lsrk_m_s']:>10.0f}  {r['frequency_hz']:.0f}  "
                      f"{r['smoothed_excursion']:+.3f}  {r['sigma_at_peak']:.3g}" for r in ok]
            lines.append(f"suggested window: --center {sug_c:.0f} --half-width {sug_h:.0f} (operator decides)")
    _emit(payload, args.json, lines)
    return 0


def cmd_plan(args) -> int:
    from science_forensics.session import plan_forensics
    plan = plan_forensics(args.reduce_session_dir, _config(args), args.science_session)
    lines = [f"blocked: {plan['blocked']}", *[f"  - {r}" for r in plan.get("blocked_reasons", [])]]
    if "n_points_available" in plan:
        lines += [f"input: {plan['campaign_id']} / {plan['reduce_session_id']} ({plan['reduce_session_status']})",
                  f"points available {plan['n_points_available']}, with enough window channels {plan['points_with_enough_window_channels']}, "
                  f"window channels per point {plan['window_channels_per_point']}",
                  f"time span {plan['time_span_minutes']:.1f} min; sidereal time: {plan['sidereal_time']}",
                  "expected outputs: " + ", ".join(plan["expected_outputs"][:8]) + ", ..."]
    _emit(plan, args.json, lines)
    return 1 if plan["blocked"] else 0


def cmd_run(args) -> int:
    from science_forensics.session import plan_forensics, run_forensics
    config = _config(args)
    plan = plan_forensics(args.reduce_session_dir, config, args.science_session)
    if plan["blocked"]:
        print("BLOCKED: " + "; ".join(plan["blocked_reasons"]), file=sys.stderr)
        return 1
    r = run_forensics(args.reduce_session_dir, config, output_root=args.output_root, science_session_dir=args.science_session)
    a = r["analysis"]
    payload = {"session_dir": r["session_dir"], "status": r["status"], "analysis_status": r["analysis_status"],
               "candidate": r["candidate"], "metrics": a.get("metrics"), "evidence": a.get("evidence")}
    lines = [f"FORENSICS {r['status']} ({r['analysis_status']})", f"session: {r['session_dir']}",
             f"candidate {r['candidate']['id']}: status {r['candidate']['status']}"]
    lines += [f"  [{e['label']}] {e['statement']}" for e in (a.get("evidence") or [])]
    _emit(payload, args.json, lines)
    return 0


def cmd_validate(args) -> int:
    from science_forensics.session import validate_forensics_session
    res = validate_forensics_session(args.session_dir)
    _emit(res, args.json, [f"forensics_integrity: {'OK' if res['ok'] else 'PROBLEMS'}", *[f"  - {p}" for p in res["problems"]]])
    return 0 if res["ok"] else 1


def cmd_compare(args) -> int:
    from science_forensics.session import compare_forensics_sessions
    res = compare_forensics_sessions(args.session_a, args.session_b)
    lines = [f"COMPARE verdict: {res['verdict']}", f"  same input: {res['same_input']}  same config: {res['same_config']}",
             f"  campaigns: {res['campaigns']}"]
    lines += [f"  frame {k}: RMS (channels) {v['rms_channels']}" for k, v in res["frame_rms_channels"].items()]
    _emit(res, args.json, lines)
    return 0 if res["verdict"] == "EQUIVALENT" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="almita_science_forensics.py")
    sub = parser.add_subparsers(dest="command", required=True)

    def window(p):
        p.add_argument("--center", type=float, required=True, help="window centre in the window frame's unit")
        p.add_argument("--half-width", type=float, required=True, help="window half-width (must be > 0)")
        p.add_argument("--window-frame", choices=("lsrk_velocity", "frequency", "channel"), default="lsrk_velocity",
                       help="unit: m/s, Hz (topocentric) or channel index")
        p.add_argument("--sign", choices=("auto", "positive", "negative"), default="auto")
        p.add_argument("--method", choices=("gaussian", "halfmax", "peak"), default="gaussian")
        p.add_argument("--feature-id", default="FEATURE-1")
        p.add_argument("--integration-window-min-m-s", type=float, default=None)
        p.add_argument("--integration-window-max-m-s", type=float, default=None)
        p.add_argument("--science-session", default=None, help="optional SCIENCE session (read-only) for the integrated-map audit")
        p.add_argument("--site-from-observer-config", nargs="?", const="observer_config.json", default=None, metavar="PATH",
                       help="observatory coordinates for sidereal time (path and sha256 recorded)")
        p.add_argument("--sort-modes", default=None, help="comma list of capture,ra,dec,galactic_l")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("inspect", help="read-only Level 1 summary; optional peak scan")
    p.add_argument("reduce_session_dir")
    p.add_argument("--scan-v-min", type=float, default=None)
    p.add_argument("--scan-v-max", type=float, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("plan", help="no writes")
    p.add_argument("reduce_session_dir"); window(p); p.set_defaults(func=cmd_plan)
    p = sub.add_parser("run", help="write a FORENSICS session")
    p.add_argument("reduce_session_dir"); window(p)
    p.add_argument("--output-root", default="data/science_forensics"); p.set_defaults(func=cmd_run)
    p = sub.add_parser("validate"); p.add_argument("session_dir"); p.add_argument("--json", action="store_true"); p.set_defaults(func=cmd_validate)
    p = sub.add_parser("compare"); p.add_argument("session_a"); p.add_argument("session_b"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("CANCELLED: interrupted (a started session is marked CANCELLED, never COMPLETED)", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
