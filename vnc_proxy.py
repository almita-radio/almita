"""DESKTOP: the Pi's desktop in the browser through ALMITA's own port and login (operator, 2026-10-17).

StellarMate already runs x11vnc (:5900) and noVNC/websockify (:6080). ALMITA's web server forwards /vnc/... to that
noVNC on localhost - its page and files over plain HTTP, and its websocket by splicing the raw TCP stream once the
handshake is forwarded - so the desktop needs no extra port and sits behind the same authentication as every ALMITA
page. The websocket handshake must also pass the same-origin check (a browser attaches cached credentials to a
websocket opened by another site). Nothing here speaks VNC: noVNC and websockify do."""
from __future__ import annotations

import http.client
import socket
import threading
from typing import Optional, Tuple
from urllib.parse import urlsplit

PREFIX = "/vnc"
UPSTREAM: Tuple[str, int] = ("127.0.0.1", 6080)
# noVNC builds its websocket URL from `path` (relative to the host): it must come back through this same prefix
ENTRY = PREFIX + "/vnc.html?path=vnc/websockify&autoconnect=true&resize=scale&reconnect=true"
_FORWARD_HTTP_HEADERS = ("Content-Type", "Last-Modified", "ETag")
_DROP_WS_HEADERS = ("host", "authorization", "cookie")


def handles(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def upstream_target(raw_path: str) -> str:
    """'/vnc/app/ui.js?x=1' -> '/app/ui.js?x=1'; the prefix alone -> '/'."""
    parts = urlsplit(raw_path)
    rest = parts.path[len(PREFIX):] or "/"
    return rest + (f"?{parts.query}" if parts.query else "")


def is_websocket(handler) -> bool:
    return (handler.headers.get("Upgrade") or "").lower() == "websocket"


def proxy_http(handler, upstream: Optional[Tuple[str, int]] = None, head_only: bool = False) -> None:
    conn = http.client.HTTPConnection(*(upstream or UPSTREAM), timeout=10)
    try:
        conn.request("GET", upstream_target(handler.path), headers={"Accept-Encoding": "identity"})
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    handler.send_response(resp.status)
    for name in _FORWARD_HTTP_HEADERS:
        value = resp.getheader(name)
        if value:
            handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if not head_only:
        handler.wfile.write(body)


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def proxy_websocket(handler, upstream: Optional[Tuple[str, int]] = None) -> None:
    """Forward the handshake to websockify, then splice both directions until either side closes. websockify itself
    answers the handshake (101) - this side never writes an HTTP response of its own."""
    upstream = upstream or UPSTREAM
    up = socket.create_connection(upstream, timeout=10)
    lines = [f"GET {upstream_target(handler.path)} HTTP/1.1", f"Host: {upstream[0]}:{upstream[1]}"]
    lines += [f"{k}: {v}" for k, v in handler.headers.items() if k.lower() not in _DROP_WS_HEADERS]
    up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    client = handler.connection
    client.settimeout(None)
    up.settimeout(None)
    back = threading.Thread(target=_pump, args=(up, client), daemon=True)
    back.start()
    _pump(client, up)
    back.join(5)
    up.close()
    handler.close_connection = True
