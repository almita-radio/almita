#!/usr/bin/env python3
"""ALMITA Orchestrator Web API: local, offline JSON API for the OBSERVE web view.

Calls the exact same core functions the CLI (almita_observe.py) calls —
observation_spec.validate_spec_dict(), observation_plan.plan_observation(),
observation_orchestrator.run_observation()/get_status()/stop_observation().
No planning/validation/safety/execution logic is duplicated here.

Every POST body is parsed as strict JSON and validated against the
observation schema before it ever reaches the core — no shell execution,
no raw command strings from request data, no eval. Static asset serving
is read-only and path-sanitized (no directory listing, no traversal).

Offline/local instrument UI: no external CDN, no cloud dependency, no
authentication infrastructure (matching almita_console_server.py's own
established no-auth-but-local-LAN model).
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import re
import signal
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlsplit

import observation_orchestrator
import observation_plan
import observation_spec

import almita_web_align
import almita_web_calibrate
import almita_web_ops
import almita_web_system

MAX_BODY_BYTES = 1_000_000
CLIENT_SOCKET_TIMEOUT_SECONDS = 60  # a stalled client must not pin a request thread forever (handler compute time is not affected)
ASSET_ROOT = Path("data/mosaic").resolve()
RESOLVED_PLAN_NAME = "observation_resolved.json"
COMPONENT = "almita_orchestrator_server"
_PLAN_LOCK = threading.Lock()  # planning renders matplotlib figures (heavy on a Pi with ~0 free swap): one at a time
ALLOWED_ASSET_SUFFIXES = {".png", ".json", ".csv"}
_ASSET_PATH_RE = re.compile(r"^/api/observe/plan-assets/(?P<rel>.+)$")

# Session/analysis directory roots the ALIGN/CALIBRATE artifact-serving
# route is allowed to read from - never anything outside these (Fase 57:
# no arbitrary filesystem path).
ALIGN_SESSION_ROOT = Path("data/alignment").resolve()
CALIBRATE_SESSION_ROOT = Path("data/calibration").resolve()

# Static app pages, served read-only from console/ (the existing
# monitoring console's own static server never sees these — see the module
# docstring on why this lives on its own port instead of extending
# almita_console_server.py's read-only, no-POST contract). OBSERVE's own
# three entries are unchanged (Fase 50) - ALIGN/CALIBRATE are additive.
CONSOLE_SOURCE = Path(__file__).resolve().parent / "console"
STATIC_FILES = {
    "/": "index_redirect",
    "/observe.html": "observe.html",
    "/observe.js": "observe.js",
    "/styles.css": "styles.css",
    "/align.html": "align.html",
    "/align.js": "align.js",
    "/calibrate.html": "calibrate.html",
    "/calibrate.js": "calibrate.js",
    # Web polish: shared helpers + the compact system-status page. Additive; no existing URL changed.
    "/common.js": "common.js",
    "/status.html": "status.html",
    "/status.js": "status.js",
    # RW operations: the real end-to-end pipeline page (preflight -> align -> calibrate -> observe -> reduce -> science)
    "/pipeline.html": "pipeline.html",
    "/pipeline.js": "pipeline.js",
    # REDUCE console: pick a capture/campaign, preview metadata + compatibility, PLAN, RUN, see results -
    # the dedicated, full-featured page (the PIPELINE panel above stays as the quick/linear path).
    "/reduce.html": "reduce.html",
    "/reduce.js": "reduce.js",
}
_STATIC_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")  # Fase 57: no path traversal via a session id
_ALIGN_SESSION_RE = re.compile(r"^/api/align/session/(?P<id>[^/]+)$")
_CALIBRATE_SESSION_RE = re.compile(r"^/api/calibrate/session/(?P<id>[^/]+)$")
_ALIGN_MODE_ACTION_RE = re.compile(r"^/api/align/(?P<action>plan|preflight|run)/(?P<mode>solar|hi)$")
_ALIGN_SYNC_RE = re.compile(r"^/api/align/sync/(?P<step>prepare|apply)/(?P<mode>solar|hi)$")
_OPS_JOB_RE = re.compile(r"^/api/ops/job/(?P<id>[A-Za-z0-9_-]{6,64})$")
_OPS_START_RE = re.compile(r"^/api/ops/start/(?P<stage>[a-z_]{3,24})$")
_OPS_STOP_RE = re.compile(r"^/api/ops/stop/(?P<id>[A-Za-z0-9_-]{6,64})$")


def _log(level: str, message: str) -> None:
    """One line: UTC timestamp, level, component, message (stdout/journal). Never large payloads."""
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {level:<5} {COMPONENT} {message[:400]}", flush=True)


def _sanitize(value: Any) -> Any:
    """JSON must never carry NaN/Infinity (invalid JSON: the browser's res.json() would throw): non-finite floats become null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _common_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "SAMEORIGIN")
    handler.send_header("Referrer-Policy", "same-origin")
    request_id = getattr(handler, "_request_id", None)
    if request_id:
        handler.send_header("X-Request-Id", request_id)


def _write_body(handler: BaseHTTPRequestHandler, data: bytes) -> None:
    if not getattr(handler, "_head_only", False):  # HEAD: same headers, no body
        handler.wfile.write(data)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(_sanitize(payload), indent=2, allow_nan=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _common_headers(handler)
    handler.end_headers()
    _write_body(handler, body)


def _error_response(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    """Uniform error body. `error` is the key the existing pages read; the rest is additive (ok/status/message/request_id)."""
    _json_response(handler, status, {"ok": False, "status": "ERROR", "error": message, "message": message,
                                      "http_status": status, "request_id": getattr(handler, "_request_id", None)})


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("invalid Content-Length header") from None
    if length <= 0 or length > MAX_BODY_BYTES:
        raise ValueError(f"request body must be 1..{MAX_BODY_BYTES} bytes")
    raw = handler.rfile.read(length)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _observe_defaults() -> Dict[str, Any]:
    config = json.loads(Path("observer_config.json").read_text(encoding="utf-8"))
    defaults = config.get("observation_defaults", {})
    return {
        "main": {
            "center_frequency_hz": defaults.get("center_frequency_hz", 1420405000),
            "sample_rate": defaults.get("sample_rate_hz", 2400000),
            "gain_db": 40.2,
            "bias_tee": True,
        },
        "grid": {
            "min_altitude_deg": defaults.get("min_altitude_deg", 30.0),
            "traversal": "SERPENTINE",
        },
        "rfi_ref": {"serial": "00000002", "gain_db": 25.0},
        "observer": config.get("observer", {}),
    }


def _validated_resolved_plan_path(raw: Any) -> str:
    """START reads whatever file it is pointed at, so the path must be a plan this app itself produced: data/mosaic/<campaign>/
    observation_resolved.json (absolute, or relative to the server's working directory). Anything else is invalid input (400),
    not an arbitrary file read, and is never interpolated into a command."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("resolved_plan_path (string) is required")
    try:
        candidate = Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("resolved_plan_path is not a valid path") from None
    if candidate.name != RESOLVED_PLAN_NAME or ASSET_ROOT not in candidate.parents:
        raise ValueError(f"resolved_plan_path must be {RESOLVED_PLAN_NAME} inside data/mosaic/<campaign>/")
    return raw


class ObserveHandler(BaseHTTPRequestHandler):
    server_version = "AlmitaOrchestratorAPI/1"
    timeout = CLIENT_SOCKET_TIMEOUT_SECONDS

    # ---- logging: UTC timestamp, level, component, request id; never bodies
    _QUIET_POLL_PATHS = ("/healthz", "/api/system/health", "/api/system/version", "/api/observe/status", "/api/align/status", "/api/calibrate/status")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # Successful polls (every open page asks every few seconds) would fill the journal: log them only when they fail.
        code = str(args[1]) if len(args) > 1 else ""
        path = urlsplit(getattr(self, "path", "") or "").path
        if getattr(self, "command", "") in ("GET", "HEAD") and code[:1] in ("2", "3") and (path in self._QUIET_POLL_PATHS or path.startswith("/api/align/session/") or path.startswith("/api/calibrate/session/")):
            return
        _log("INFO", f"req={getattr(self, '_request_id', '-')} {self.address_string()} {format % args}")

    def _begin(self) -> str:
        self._request_id = uuid.uuid4().hex[:8]
        self._head_only = False
        return urlsplit(self.path).path            # routing ignores the query string (?v=..., bookmarks)

    def _fail(self, exc: BaseException, status: int, public: str) -> None:
        """Traceback -> backend log only (with the request id); the browser gets a short message + the id."""
        _log("ERROR", f"req={self._request_id} {self.command} {self.path} -> {status} {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stdout)
        _error_response(self, status, f"{public} (request id {self._request_id})")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()
        # do_GET set the flag after _begin(); nothing else to do - the body writer honours it

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        path = self._begin()
        self._head_only = self.command == "HEAD"
        try:
            if path == "/healthz":
                return _json_response(self, 200, almita_web_system.liveness())
            if path == "/api/system/health":
                return _json_response(self, 200, {"ok": True, "status": "OK", "message": "read-only; service, dependency and operational layers are separate",
                                                   "data": almita_web_system.cached_health()})
            if path == "/api/system/version":
                return _json_response(self, 200, {"ok": True, "status": "OK", "data": almita_web_system.version_info()})
            if path == "/api/observe/defaults":
                return _json_response(self, 200, _observe_defaults())
            if path == "/api/observe/status":
                status = observation_orchestrator.get_status()
                return _json_response(self, 200, status)
            if path == "/api/align/status":
                return _json_response(self, 200, almita_web_align.get_status())
            if path == "/api/calibrate/status":
                return _json_response(self, 200, almita_web_calibrate.get_status())
            if path == "/api/ops/jobs":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.list_jobs()})
            if path == "/api/ops/mount":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.read_mount()})
            if path == "/api/ops/campaigns":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.campaigns()})
            if path == "/api/ops/reduce/captures":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_list_captures()})
            if path == "/api/ops/reduce/inspect_capture":
                return self._ops_reduce_inspect_capture()
            if path == "/api/ops/reduce/campaigns":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_list_campaigns()})
            if path == "/api/ops/reduce/inspect_campaign":
                return self._ops_reduce_inspect_campaign()
            if path == "/api/ops/reduce/compatibility":
                return self._ops_reduce_compatibility()
            if path == "/api/ops/reduce/campaign_calibration_preview":
                return self._ops_reduce_campaign_calibration_preview()
            if path == "/api/ops/reduce/point":
                return self._ops_reduce_point()
            if path == "/api/ops/align/defaults":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.align_defaults()})
            if path == "/api/ops/align/sky":
                return self._ops_align_sky()
            if path == "/api/ops/file":
                return self._ops_file()
            ops_job = _OPS_JOB_RE.match(path)
            if ops_job:
                try:
                    return _json_response(self, 200, {"ok": True, "data": almita_web_ops.get_job(ops_job.group("id"), tail=int(parse_qs(urlsplit(self.path).query).get("tail", ["80"])[0]))})
                except FileNotFoundError:
                    return _error_response(self, 404, "unknown job")
            align_session = _ALIGN_SESSION_RE.match(path)
            if align_session:
                return self._align_session(align_session.group("id"))
            calibrate_session = _CALIBRATE_SESSION_RE.match(path)
            if calibrate_session:
                return self._calibrate_session(calibrate_session.group("id"))
            if path in STATIC_FILES:
                return self._serve_static(path)
            asset_match = _ASSET_PATH_RE.match(path)
            if asset_match:
                return self._serve_asset(asset_match.group("rel"))
            return self._not_found()
        except OSError as exc:  # a file/socket the route depends on is missing or unreadable: dependency problem, not a code bug
            self._fail(exc, 503, f"dependency unavailable ({type(exc).__name__})")
        except Exception as exc:  # noqa: BLE001 - never crash the server on a bad request
            self._fail(exc, 500, f"unexpected error ({type(exc).__name__})")

    def _not_found(self) -> None:
        if urlsplit(self.path).path.startswith("/api/"):
            return _error_response(self, 404, "not found")
        self.send_error(404, "not found")

    def _align_session(self, session_id: str) -> None:
        if not _SESSION_ID_RE.match(session_id):
            return _error_response(self, 400, "invalid session id")
        return _json_response(self, 200, almita_web_align.get_session(session_id))

    def _calibrate_session(self, session_id: str) -> None:
        if not _SESSION_ID_RE.match(session_id):
            return _error_response(self, 400, "invalid session id")
        return _json_response(self, 200, almita_web_calibrate.get_session(session_id))

    def _serve_static(self, path: str) -> None:
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/observe.html")
            _common_headers(self)
            self.end_headers()
            return
        filename = STATIC_FILES[path]
        candidate = (CONSOLE_SOURCE / filename).resolve()
        if CONSOLE_SOURCE != candidate.parent or not candidate.is_file():
            return self.send_error(404, "not found")
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _STATIC_CONTENT_TYPES[candidate.suffix])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        _common_headers(self)
        self.end_headers()
        _write_body(self, data)

    def do_POST(self) -> None:  # noqa: N802
        path = self._begin()
        try:
            if path == "/api/observe/plan":
                return self._handle_plan()
            if path == "/api/observe/start":
                return self._handle_start()
            if path == "/api/observe/stop":
                return self._handle_stop()
            if path == "/api/ops/preflight":
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.preflight()})
            ops_start = _OPS_START_RE.match(path)
            if ops_start:
                body = _read_json_body(self)
                _log("INFO", f"req={self._request_id} OPS START {ops_start.group('stage')} params={json.dumps(body.get('params') or {})[:200]}")
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.start(ops_start.group("stage"), body.get("params") or {}, confirm=body.get("confirm"))})
            ops_stop = _OPS_STOP_RE.match(path)
            if ops_stop:
                if _read_json_body(self).get("confirm") is not True:
                    raise ValueError("confirm: true is required - STOP does not proceed implicitly")
                _log("INFO", f"req={self._request_id} OPS STOP {ops_stop.group('id')}")
                return _json_response(self, 200, {"ok": True, "data": almita_web_ops.stop(ops_stop.group("id"))})

            align_action = _ALIGN_MODE_ACTION_RE.match(path)
            if align_action:
                return self._handle_align_action(align_action.group("action"), align_action.group("mode"))
            align_sync = _ALIGN_SYNC_RE.match(path)
            if align_sync:
                body = _read_json_body(self)
                fn = almita_web_align.sync_prepare if align_sync.group("step") == "prepare" else almita_web_align.sync_apply
                return _json_response(self, 200, fn(align_sync.group("mode"), body))
            if path == "/api/align/replay":
                return _json_response(self, 200, almita_web_align.replay(_read_json_body(self)))
            if path == "/api/align/compare":
                return _json_response(self, 200, almita_web_align.compare(_read_json_body(self)))

            if path == "/api/calibrate/plan":
                return _json_response(self, 200, almita_web_calibrate.plan(_read_json_body(self)))
            if path == "/api/calibrate/preflight":
                return _json_response(self, 200, almita_web_calibrate.preflight(_read_json_body(self)))
            if path == "/api/calibrate/run":
                return _json_response(self, 200, almita_web_calibrate.run_simulation(_read_json_body(self)))
            if path == "/api/calibrate/replay":
                return _json_response(self, 200, almita_web_calibrate.replay(_read_json_body(self)))
            if path == "/api/calibrate/compare":
                return _json_response(self, 200, almita_web_calibrate.compare(_read_json_body(self)))
            if path == "/api/calibrate/profile/build":
                return _json_response(self, 200, almita_web_calibrate.profile_build(_read_json_body(self)))

            return self._not_found()
        except (ValueError, json.JSONDecodeError) as exc:
            _error_response(self, 400, str(exc))
        except observation_spec.ObservationSpecError as exc:
            _error_response(self, 400, f"invalid spec: {exc}")
        except observation_plan.ObservationPlanError as exc:
            _error_response(self, 409, f"blocked: {exc}")
        except observation_orchestrator.OrchestratorError as exc:
            _error_response(self, 409, str(exc))
        except almita_web_ops.OpsBlocked as exc:
            _error_response(self, 409, f"blocked: {exc}")
        except FileNotFoundError as exc:
            _error_response(self, 404, f"not found: {exc}")
        except OSError as exc:
            self._fail(exc, 503, f"dependency unavailable ({type(exc).__name__})")
        except Exception as exc:  # noqa: BLE001
            self._fail(exc, 500, f"unexpected error ({type(exc).__name__})")

    def do_PUT(self) -> None:  # noqa: N802
        self._begin()
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST")
        body = json.dumps({"ok": False, "status": "ERROR", "error": "read/JSON-write only", "http_status": 405,
                           "request_id": self._request_id}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        _common_headers(self)
        self.end_headers()
        self.wfile.write(body)

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    def _handle_plan(self) -> None:
        body = _read_json_body(self)
        spec = observation_spec.validate_spec_dict(body)
        if not _PLAN_LOCK.acquire(blocking=False):
            return _error_response(self, 409, "planning already in progress (one plan at a time); wait for it to finish")
        try:
            plan = observation_plan.plan_observation(spec)
        finally:
            _PLAN_LOCK.release()
        _json_response(self, 200, plan)

    def _handle_align_action(self, action: str, mode: str) -> None:
        body = _read_json_body(self)
        if action == "plan":
            return _json_response(self, 200, almita_web_align.plan(mode, body))
        if action == "run":
            return _json_response(self, 200, almita_web_align.run_simulation(mode, body))
        # "preflight" is the only async one - each HTTP request already
        # runs in its own thread (ThreadingHTTPServer), so asyncio.run()
        # here starts a fresh event loop local to this thread and never
        # conflicts with another request's loop (this server has no
        # already-running event loop of its own to nest inside).
        import asyncio
        result = asyncio.run(almita_web_align.preflight(mode, body))
        return _json_response(self, 200, result)

    def _handle_start(self) -> None:
        body = _read_json_body(self)
        raw_path, confirm = body.get("resolved_plan_path"), body.get("confirm")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("resolved_plan_path (string) is required")
        if confirm is not True:
            raise ValueError("confirm: true is required — START does not proceed implicitly")
        resolved_plan_path = _validated_resolved_plan_path(raw_path)
        held = almita_web_ops.busy_reason()
        if held:
            return _error_response(self, 409, f"blocked: {held}")
        # Optional gain-pilot stage: if it was never used for this grid session, this is a no-op (unchanged
        # OBSERVE behaviour). If it WAS used, it must have reached READY against THIS exact resolved plan
        # (same config hash, same gain) - never let a stale/partial/mismatched pilot silently pass.
        import observation_gain_pilot
        pilot_block = observation_gain_pilot.check_ready_for_grid_start(Path(resolved_plan_path))
        if pilot_block:
            return _error_response(self, 409, f"blocked: gain pilot: {pilot_block}")
        # The browser's explicit START click + confirmation dialog IS the
        # deliberate operator GO; equivalent to the CLI's explicit --yes.
        # Backend is the authority on conflicts: run_observation holds an flock and refuses (409) when a run is already active.
        _log("INFO", f"req={self._request_id} START requested plan={resolved_plan_path}")
        try:
            result = observation_orchestrator.run_observation(resolved_plan_path, yes=True)
        except FileNotFoundError:
            return _error_response(self, 404, "resolved plan not found — plan again")
        _json_response(self, 200, result)

    def _handle_stop(self) -> None:
        body = _read_json_body(self)
        if body.get("confirm") is not True:
            raise ValueError("confirm: true is required — STOP does not proceed implicitly")
        _log("INFO", f"req={self._request_id} STOP requested")
        result = observation_orchestrator.stop_observation(confirm=True)
        _json_response(self, 200, result)

    def _ops_align_sky(self) -> None:
        q = parse_qs(urlsplit(self.path).query)
        mode = (q.get("mode") or ["hi"])[0]
        if mode not in ("hi", "solar"):
            return _error_response(self, 400, "mode must be hi or solar")
        try:
            radii = [float(x) for x in (q.get("ring_radii") or ["5.0,2.0,0.6"])[0].split(",") if x.strip()]
            ring_points = int((q.get("ring_points") or ["16"])[0])
            min_elevation = float((q.get("min_elevation") or ["20"])[0])
            beam_fwhm = float((q.get("beam_fwhm") or ["20"])[0])
            capture_time = float((q.get("capture_time") or ["20"])[0])
        except ValueError:
            return _error_response(self, 400, "ring_radii/ring_points/min_elevation/beam_fwhm/capture_time must be numbers")
        if (not radii or any(r <= 0 for r in radii) or not (3 <= ring_points <= 64)
                or not (0 <= min_elevation <= 89) or not (0.1 <= beam_fwhm <= 90) or not (0.5 <= capture_time <= 120)):
            return _error_response(self, 400, "ring_radii/ring_points/min_elevation/beam_fwhm/capture_time out of range")
        return _json_response(self, 200, {"ok": True,
            "data": almita_web_ops.align_sky(mode, radii, ring_points, min_elevation, beam_fwhm, capture_time)})

    def _ops_reduce_inspect_capture(self) -> None:
        rel = (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]
        try:
            return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_inspect_capture(rel)})
        except (ValueError, FileNotFoundError) as exc:
            return _error_response(self, 400, str(exc))

    def _ops_reduce_inspect_campaign(self) -> None:
        rel = (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]
        try:
            return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_inspect_campaign(rel)})
        except (ValueError, FileNotFoundError) as exc:
            return _error_response(self, 400, str(exc))

    def _ops_reduce_compatibility(self) -> None:
        q = parse_qs(urlsplit(self.path).query)
        capture = (q.get("capture") or [None])[0]
        campaign_dir = (q.get("campaign_dir") or [None])[0]
        profile = (q.get("profile") or [None])[0]
        if not profile:
            return _error_response(self, 400, "profile is required")
        try:
            return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_check_compatibility(
                capture_rel=capture, campaign_rel=campaign_dir, profile_rel=profile)})
        except (ValueError, FileNotFoundError) as exc:
            return _error_response(self, 400, str(exc))

    def _ops_reduce_campaign_calibration_preview(self) -> None:
        q = parse_qs(urlsplit(self.path).query)
        campaign_dir = (q.get("campaign_dir") or [None])[0]
        profile = (q.get("profile") or [None])[0]
        if not campaign_dir or not profile:
            return _error_response(self, 400, "campaign_dir and profile are both required")
        try:
            return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_campaign_calibration_preview(campaign_dir, profile)})
        except (ValueError, FileNotFoundError) as exc:
            return _error_response(self, 400, str(exc))

    def _ops_reduce_point(self) -> None:
        q = parse_qs(urlsplit(self.path).query)
        session_dir = (q.get("session_dir") or [""])[0]
        try:
            point_index = int((q.get("point_index") or ["0"])[0])
        except ValueError:
            return _error_response(self, 400, "point_index must be an integer")
        try:
            return _json_response(self, 200, {"ok": True, "data": almita_web_ops.reduce_point_result(session_dir, point_index)})
        except (ValueError, FileNotFoundError, KeyError, OSError) as exc:
            return _error_response(self, 400, str(exc))

    def _ops_file(self) -> None:
        rel = (parse_qs(urlsplit(self.path).query).get("path") or [""])[0]
        try:
            path, ctype = almita_web_ops.resolve_file(rel)
        except FileNotFoundError:
            return _error_response(self, 404, "not found")
        except ValueError as exc:
            return _error_response(self, 400, str(exc))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        _common_headers(self)
        self.end_headers()
        _write_body(self, data)

    def _serve_asset(self, rel: str) -> None:
        try:
            candidate = (ASSET_ROOT / unquote(rel)).resolve()
        except (ValueError, OSError, RuntimeError):  # NUL byte or unresolvable path
            return _error_response(self, 400, "invalid asset path")
        if ASSET_ROOT not in candidate.parents and candidate != ASSET_ROOT:
            return self.send_error(403, "path outside asset root")
        if candidate.suffix.lower() not in ALLOWED_ASSET_SUFFIXES or not candidate.is_file():
            return self.send_error(404, "not found")
        data = candidate.read_bytes()
        content_type = {"png": "image/png", "json": "application/json", "csv": "text/csv"}[candidate.suffix.lstrip(".").lower()]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        _common_headers(self)
        self.end_headers()
        _write_body(self, data)


class ObserveServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # a closed tab / aborted fetch is normal on a resident server
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            _log("INFO", f"client {client_address} disconnected ({exc.__class__.__name__})")
            return
        super().handle_error(request, client_address)


def make_server(host: str = "0.0.0.0", port: int = 8090) -> ThreadingHTTPServer:
    return ObserveServer((host, port), ObserveHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="ALMITA Orchestrator Web API (local, offline)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    try:
        server = make_server(args.host, args.port)
    except OSError as exc:
        reason = "port already in use (another instance or service holds it; nothing was killed)" if exc.errno == errno.EADDRINUSE else str(exc)
        _log("ERROR", f"cannot bind {args.host}:{args.port}: {reason}")
        return 2

    def request_stop(signum, frame) -> None:
        # serve_forever runs in this (main) thread: shutdown() must be called from another one
        threading.Thread(target=server.shutdown, name="web-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    print(f"ALMITA ORCHESTRATOR API START  {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        print("ALMITA ORCHESTRATOR API STOP", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
