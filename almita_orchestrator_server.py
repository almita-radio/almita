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
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import observation_orchestrator
import observation_plan
import observation_spec

import almita_web_align
import almita_web_calibrate

MAX_BODY_BYTES = 1_000_000
ASSET_ROOT = Path("data/mosaic").resolve()
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
}
_STATIC_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css"}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")  # Fase 57: no path traversal via a session id
_ALIGN_SESSION_RE = re.compile(r"^/api/align/session/(?P<id>[^/]+)$")
_CALIBRATE_SESSION_RE = re.compile(r"^/api/calibrate/session/(?P<id>[^/]+)$")
_ALIGN_MODE_ACTION_RE = re.compile(r"^/api/align/(?P<action>plan|preflight|run)/(?P<mode>solar|hi)$")
_ALIGN_SYNC_RE = re.compile(r"^/api/align/sync/(?P<step>prepare|apply)/(?P<mode>solar|hi)$")


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
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


class ObserveHandler(BaseHTTPRequestHandler):
    server_version = "AlmitaOrchestratorAPI/1"

    def log_message(self, format: str, *args) -> None:  # quieter, consistent stdout logging
        print(f"[almita_orchestrator_server] {self.address_string()} - {format % args}")

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        try:
            if self.path == "/api/observe/defaults":
                return _json_response(self, 200, _observe_defaults())
            if self.path == "/api/observe/status":
                status = observation_orchestrator.get_status()
                return _json_response(self, 200, status)
            if self.path == "/api/align/status":
                return _json_response(self, 200, almita_web_align.get_status())
            if self.path == "/api/calibrate/status":
                return _json_response(self, 200, almita_web_calibrate.get_status())
            align_session = _ALIGN_SESSION_RE.match(self.path)
            if align_session:
                return self._align_session(align_session.group("id"))
            calibrate_session = _CALIBRATE_SESSION_RE.match(self.path)
            if calibrate_session:
                return self._calibrate_session(calibrate_session.group("id"))
            if self.path in STATIC_FILES:
                return self._serve_static(self.path)
            asset_match = _ASSET_PATH_RE.match(self.path)
            if asset_match:
                return self._serve_asset(asset_match.group("rel"))
            self.send_error(404, "not found")
        except Exception as exc:  # noqa: BLE001 - never crash the server on a bad request
            _json_response(self, 500, {"error": f"{type(exc).__name__}: {exc}"})

    def _align_session(self, session_id: str) -> None:
        if not _SESSION_ID_RE.match(session_id):
            return _json_response(self, 400, {"error": "invalid session id"})
        return _json_response(self, 200, almita_web_align.get_session(session_id))

    def _calibrate_session(self, session_id: str) -> None:
        if not _SESSION_ID_RE.match(session_id):
            return _json_response(self, 400, {"error": "invalid session id"})
        return _json_response(self, 200, almita_web_calibrate.get_session(session_id))

    def _serve_static(self, path: str) -> None:
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/observe.html")
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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/observe/plan":
                return self._handle_plan()
            if self.path == "/api/observe/start":
                return self._handle_start()
            if self.path == "/api/observe/stop":
                return self._handle_stop()

            align_action = _ALIGN_MODE_ACTION_RE.match(self.path)
            if align_action:
                return self._handle_align_action(align_action.group("action"), align_action.group("mode"))
            align_sync = _ALIGN_SYNC_RE.match(self.path)
            if align_sync:
                body = _read_json_body(self)
                fn = almita_web_align.sync_prepare if align_sync.group("step") == "prepare" else almita_web_align.sync_apply
                return _json_response(self, 200, fn(align_sync.group("mode"), body))
            if self.path == "/api/align/replay":
                return _json_response(self, 200, almita_web_align.replay(_read_json_body(self)))
            if self.path == "/api/align/compare":
                return _json_response(self, 200, almita_web_align.compare(_read_json_body(self)))

            if self.path == "/api/calibrate/plan":
                return _json_response(self, 200, almita_web_calibrate.plan(_read_json_body(self)))
            if self.path == "/api/calibrate/preflight":
                return _json_response(self, 200, almita_web_calibrate.preflight(_read_json_body(self)))
            if self.path == "/api/calibrate/run":
                return _json_response(self, 200, almita_web_calibrate.run_simulation(_read_json_body(self)))
            if self.path == "/api/calibrate/replay":
                return _json_response(self, 200, almita_web_calibrate.replay(_read_json_body(self)))
            if self.path == "/api/calibrate/compare":
                return _json_response(self, 200, almita_web_calibrate.compare(_read_json_body(self)))
            if self.path == "/api/calibrate/profile/build":
                return _json_response(self, 200, almita_web_calibrate.profile_build(_read_json_body(self)))

            self.send_error(404, "not found")
        except (ValueError, json.JSONDecodeError) as exc:
            _json_response(self, 400, {"error": str(exc)})
        except observation_spec.ObservationSpecError as exc:
            _json_response(self, 400, {"error": f"invalid spec: {exc}"})
        except observation_plan.ObservationPlanError as exc:
            _json_response(self, 409, {"error": f"blocked: {exc}"})
        except observation_orchestrator.OrchestratorError as exc:
            _json_response(self, 409, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_PUT(self) -> None:  # noqa: N802
        self.send_error(405, "read/JSON-write only")

    do_DELETE = do_PUT
    do_PATCH = do_PUT

    def _handle_plan(self) -> None:
        body = _read_json_body(self)
        spec = observation_spec.validate_spec_dict(body)
        plan = observation_plan.plan_observation(spec)
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
        resolved_plan_path = body.get("resolved_plan_path")
        confirm = body.get("confirm")
        if not isinstance(resolved_plan_path, str) or not resolved_plan_path:
            raise ValueError("resolved_plan_path (string) is required")
        if confirm is not True:
            raise ValueError("confirm: true is required — START does not proceed implicitly")
        # The browser's explicit START click + confirmation dialog IS the
        # deliberate operator GO; equivalent to the CLI's explicit --yes.
        result = observation_orchestrator.run_observation(resolved_plan_path, yes=True)
        _json_response(self, 200, result)

    def _handle_stop(self) -> None:
        body = _read_json_body(self)
        if body.get("confirm") is not True:
            raise ValueError("confirm: true is required — STOP does not proceed implicitly")
        result = observation_orchestrator.stop_observation(confirm=True)
        _json_response(self, 200, result)

    def _serve_asset(self, rel: str) -> None:
        from urllib.parse import unquote
        candidate = (ASSET_ROOT / unquote(rel)).resolve()
        if ASSET_ROOT not in candidate.parents and candidate != ASSET_ROOT:
            return self.send_error(403, "path outside asset root")
        if candidate.suffix.lower() not in ALLOWED_ASSET_SUFFIXES or not candidate.is_file():
            return self.send_error(404, "not found")
        data = candidate.read_bytes()
        content_type = {"png": "image/png", "json": "application/json", "csv": "text/csv"}[candidate.suffix.lstrip(".").lower()]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str = "0.0.0.0", port: int = 8090) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), ObserveHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="ALMITA Orchestrator Web API (local, offline)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"ALMITA ORCHESTRATOR API START  {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        print("ALMITA ORCHESTRATOR API STOP", flush=True)


if __name__ == "__main__":
    main()
