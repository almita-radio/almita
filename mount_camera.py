"""Single-upstream MJPEG relay for the ESP32-CAM mount camera (MONITOR's MOUNT CAMERA tile).

The ESP32-CAM's /stream endpoint accepts exactly ONE client at a time. This module keeps AT MOST one
real connection to it open, shared by every MONITOR viewer through ALMITA's own :8088 console server -
the browser never talks to the camera's private LAN address directly, and a second tab/browser never
opens a second upstream connection (verified: see the relay's own subscriber count below).

Frames are relayed as raw bytes only - never decoded, resized or re-encoded. This is a byte-level
passthrough of the camera's own multipart/x-mixed-replace stream (its own boundary marker included),
not an image pipeline: no PIL/OpenCV/ffmpeg, nothing here ever understands a JPEG frame's contents.

No mount movement, no SDR capture, no other module touched - this file and its wiring into
almita_console_server.py are the entire change.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "data" / "runtime" / "mount_camera_config.json"
DEFAULT_STREAM_URL = "http://192.168.1.164:81/stream"

IDLE_TIMEOUT_S = 15.0       # no viewers for this long -> close the one upstream connection ("breve tiempo de inactividad")
CONNECT_TIMEOUT_S = 6.0     # also the read timeout on the upstream socket (urllib applies it to the whole connection)
CONNECTED_WAIT_TIMEOUT_S = 8.0   # how long a NEW viewer's request waits to learn if the upstream connected, before answering
READ_CHUNK = 4096
RETRY_COOLDOWN_S = 3.0      # after the camera drops the connection on its own, a minimum gap before reconnecting - avoids a hot loop against a genuinely failing camera
MAX_CONNECT_RETRIES = 4     # bounded immediate retries on a CONNECT failure before reporting ERROR - absorbs the single-client-slot-not-yet-freed race, still gives up on a genuinely unreachable camera


def _read_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _write_config(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.part")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)


def valid_stream_url(url: str) -> bool:
    return isinstance(url, str) and 8 <= len(url) <= 500 and (url.startswith("http://") or url.startswith("https://"))


class _Subscriber:
    """One downstream MONITOR viewer's private byte queue - the relay pushes the SAME bytes read from
    the camera into every currently-subscribed queue, so N viewers share exactly one upstream read."""

    def __init__(self) -> None:
        self._chunks: List[bytes] = []
        self._cond = threading.Condition()
        self._closed = False

    def push(self, data: bytes) -> None:
        with self._cond:
            self._chunks.append(data)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()

    def read(self, timeout: float = 30.0) -> Optional[bytes]:
        """None -> the relay is done with this subscriber (closed); b"" -> a heartbeat wake with
        nothing new yet (caller should just loop - lets it notice a dead client socket promptly even
        during a quiet stretch); otherwise the next chunk of raw camera bytes."""
        with self._cond:
            if not self._chunks and not self._closed:
                self._cond.wait(timeout=timeout)
            if self._closed and not self._chunks:
                return None
            return self._chunks.pop(0) if self._chunks else b""


class MountCameraRelay:
    """Process-wide singleton (one instance per almita_console_server.py process) owning the single
    upstream connection to the camera and fanning its bytes out to every MONITOR viewer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: Dict[int, _Subscriber] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_upstream = threading.Event()
        self._connected_or_failed = threading.Event()
        self._idle_timer: Optional[threading.Timer] = None
        self._state = "IDLE"            # IDLE | CONNECTING | STREAMING | ERROR
        self._last_error: Optional[str] = None
        self._last_connect_attempt_utc: Optional[str] = None
        self._last_frame_utc: Optional[str] = None
        self._content_type = "multipart/x-mixed-replace"

    # -- config -------------------------------------------------------------------------------------
    def get_url(self) -> str:
        return _read_config().get("stream_url") or DEFAULT_STREAM_URL

    def set_url(self, url: str) -> None:
        cfg = _read_config()
        cfg["stream_url"] = url
        cfg["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _write_config(cfg)
        # Takes effect on the NEXT (re)connect only - never redirects an in-flight relay under a
        # viewer's feet. If nothing is currently connecting/streaming, next subscribe() picks it up.

    # -- read-only status (drives the web UI's connection indicator) --------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {"state": self._state, "last_error": self._last_error, "viewers": len(self._subscribers),
                    "configured_url": self.get_url(), "default_url": DEFAULT_STREAM_URL,
                    "last_connect_attempt_utc": self._last_connect_attempt_utc, "last_frame_utc": self._last_frame_utc}

    @property
    def content_type(self) -> str:
        with self._lock:
            return self._content_type

    # -- subscriber lifecycle -------------------------------------------------------------------------
    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        with self._lock:
            self._subscribers[id(sub)] = sub
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None
            already_connecting_or_up = self._thread is not None and self._thread.is_alive()
            if not already_connecting_or_up:
                self._connected_or_failed.clear()
                self._stop_upstream.clear()
                self._state = "CONNECTING"
                self._thread = threading.Thread(target=self._run_upstream, name="mount-camera-relay", daemon=True)
                self._thread.start()
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subscribers.pop(id(sub), None)
            sub.close()
            if not self._subscribers and self._thread is not None:
                if self._idle_timer:
                    self._idle_timer.cancel()
                self._idle_timer = threading.Timer(IDLE_TIMEOUT_S, self._idle_shutdown)
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def wait_connected_or_failed(self, timeout: float) -> None:
        self._connected_or_failed.wait(timeout=timeout)

    def _idle_shutdown(self) -> None:
        with self._lock:
            if self._subscribers:
                return   # a viewer arrived during the timer window - nothing to do
            self._stop_upstream.set()

    def _broadcast(self, data: bytes) -> None:
        with self._lock:
            subs = list(self._subscribers.values())
        for sub in subs:
            sub.push(data)

    def _run_upstream(self) -> None:
        """The ONLY thread that ever opens a real connection to the camera - subscribe() above never
        starts a second one while this is alive. Retries a failed CONNECT a bounded number of times
        with a short backoff before giving up: a real, observed race (verified against both a mock
        single-client camera and reasoned from real TCP behaviour) is that THIS relay's own previous
        connection can close on the client side a little before the camera's single-client slot is
        actually freed on ITS side, so an immediate reconnect can be legitimately refused even though
        this relay itself never had two connections open at once. A few short, sequential retries
        absorb that race without ever opening a second connection concurrently."""
        connect_attempt = 0
        try:
            while True:
                connect_attempt += 1
                connected = self._stream_once()
                with self._lock:
                    still_watching = bool(self._subscribers)
                    stop_requested = self._stop_upstream.is_set()
                if stop_requested or not still_watching:
                    break
                if connected:
                    connect_attempt = 0     # reached STREAMING at least once - the camera later closing is not a "failure", reset the budget
                elif connect_attempt >= MAX_CONNECT_RETRIES:
                    self._connected_or_failed.set()   # giving up - only NOW signal a waiting viewer, with the final ERROR state
                    break
                time.sleep(RETRY_COOLDOWN_S)
        finally:
            with self._lock:
                self._thread = None
                still_watching = bool(self._subscribers)
                subs_to_close = [] if still_watching else list(self._subscribers.values())
            for sub in subs_to_close:
                sub.close()
            self._connected_or_failed.set()   # safety net: never leave a waiter blocked past this thread's own end

    def _stream_once(self) -> bool:
        """One real connection attempt, relayed until it ends. Returns True if it ever reached
        STREAMING (even if the camera later dropped it on its own), False if the CONNECT itself
        failed - the caller (_run_upstream) decides whether to retry or give up, and whether/when to
        signal _connected_or_failed - a transient failure here must NOT wake a waiting viewer early
        with a spurious error while a retry is still coming."""
        url = self.get_url()
        with self._lock:
            self._last_connect_attempt_utc = datetime.now(timezone.utc).isoformat()
        connected = False
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ALMITA-mount-camera-relay/1"})
            with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S) as resp:
                ctype = resp.headers.get("Content-Type") or "multipart/x-mixed-replace"
                with self._lock:
                    self._content_type = ctype
                    self._state = "STREAMING"
                    self._last_error = None
                connected = True
                self._connected_or_failed.set()
                while not self._stop_upstream.is_set():
                    # read1(), not read(): HTTPResponse.read(amt) is a plain io.BufferedReader read, which
                    # blocks until READ_CHUNK bytes actually accumulate (real, found live: with small MJPEG
                    # frames this stalled the stop_upstream check for several seconds at a time, since the
                    # loop only re-checks the flag between read() calls). read1() returns as soon as the
                    # underlying socket has ANY data, up to READ_CHUNK - the loop notices a stop/idle request
                    # within about one frame interval instead.
                    chunk = resp.read1(READ_CHUNK)
                    if not chunk:
                        break
                    with self._lock:
                        self._last_frame_utc = datetime.now(timezone.utc).isoformat()
                    self._broadcast(chunk)
        except Exception as exc:  # noqa: BLE001 - a camera/network fault is an expected real-world case, not a bug
            with self._lock:
                self._state = "ERROR"
                self._last_error = f"{type(exc).__name__}: {exc}"
        else:
            # Exited the read loop with no exception: either a deliberate stop (idle timeout - the state
            # really is IDLE now) or the camera closed its end on its own (state stays as-is; the caller's
            # retry loop will immediately try to reconnect if viewers remain, or this thread ends and a
            # future subscribe() starts fresh). Previously inverted (checked `not ... is_set()`), which
            # meant a deliberate idle stop never actually showed as IDLE in /mount_camera/status - found
            # live: last_frame_utc stopped advancing right on schedule, but state stayed "STREAMING".
            if self._stop_upstream.is_set():
                with self._lock:
                    self._state = "IDLE"
        return connected


RELAY = MountCameraRelay()
