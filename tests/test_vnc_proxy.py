"""DESKTOP through ALMITA's own port (2026-10-17): /vnc/... is forwarded to noVNC/websockify on localhost - files over
HTTP, the websocket spliced after its handshake - behind the same login, and the websocket only same-origin.
No real VNC: a fake upstream on a random loopback port stands in for websockify."""
import http.client
import socket
import threading

import pytest

import almita_orchestrator_server as server_mod
import vnc_proxy


class FakeWebsockify:
    """Answers GET with the path it saw; a websocket handshake with 101, then echoes bytes. Records the raw requests."""
    def __init__(self):
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(8)
        self.port = self.sock.getsockname()[1]; self.requests = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(4096)
            if not d:
                return
            buf += d
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        self.requests.append(head)
        if "upgrade: websocket" in head.lower():
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                d = c.recv(4096)
                if not d:
                    break
                c.sendall(b"ECHO:" + d)
        else:
            body = ("UPSTREAM " + head.split(" ")[1]).encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        c.close()


class _Auth:
    def configured(self): return True
    def check(self, header): return header == "Basic dTpw"           # u:p


@pytest.fixture
def web(tmp_path, monkeypatch):
    up = FakeWebsockify()
    monkeypatch.setattr(vnc_proxy, "UPSTREAM", ("127.0.0.1", up.port))

    def start(auth=None):
        httpd = server_mod.make_server("127.0.0.1", 0, authenticator=auth, public_root=tmp_path)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]
    servers = []
    yield up, start
    for s in servers:
        s.shutdown(); s.server_close()
    up.sock.close()


def _get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def _ws(port, origin, auth=None):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    req = (f"GET /vnc/websockify HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n"
           + (f"Authorization: {auth}\r\n" if auth else "") + "\r\n")
    s.sendall(req.encode())
    return s


def test_entry_redirects_to_novnc_with_its_websocket_path_under_vnc(web):
    up, start = web
    status, headers, _ = _get(start(), "/vnc/")
    assert status == 302 and headers["Location"] == vnc_proxy.ENTRY and "path=vnc/websockify" in headers["Location"]


def test_novnc_files_come_through_the_same_port(web):
    up, start = web
    status, headers, body = _get(start(), "/vnc/app/ui.js?v=1")
    assert status == 200 and body == b"UPSTREAM /app/ui.js?v=1" and headers["Content-Type"] == "text/html"


def test_websocket_is_spliced_same_origin_without_forwarding_credentials(web):
    up, start = web
    port = start()
    s = _ws(port, f"http://127.0.0.1:{port}", auth="Basic secret")
    head = s.recv(4096)
    assert head.startswith(b"HTTP/1.1 101")
    s.sendall(b"RFB 003.008\n")
    assert s.recv(4096) == b"ECHO:RFB 003.008\n"                     # bytes flow both ways after the handshake
    s.close()
    ws_req = next(r for r in up.requests if "websocket" in r.lower())
    assert ws_req.startswith("GET /websockify HTTP/1.1") and "authorization" not in ws_req.lower()


def test_cross_origin_websocket_is_refused(web):
    up, start = web
    s = _ws(start(), "http://evil.example")
    assert s.recv(4096).split(b"\r\n", 1)[0].endswith(b" 403 Forbidden")
    assert not any("websocket" in r.lower() for r in up.requests)


def test_desktop_needs_the_almita_login(web):
    up, start = web
    port = start(_Auth())
    assert _get(port, "/vnc/vnc.html")[0] == 401
    assert _get(port, "/vnc/vnc.html", {"Authorization": "Basic dTpw"})[0] == 200
    assert not any("vnc.html" in r for r in up.requests[:-1])          # the refused request never reached noVNC


def test_novnc_down_is_a_clear_502(web, monkeypatch):
    up, start = web
    port = start()
    dead = socket.socket(); dead.bind(("127.0.0.1", 0)); dead_port = dead.getsockname()[1]; dead.close()
    monkeypatch.setattr(vnc_proxy, "UPSTREAM", ("127.0.0.1", dead_port))
    status, _, body = _get(port, "/vnc/vnc.html")
    assert status == 502 and b"DESKTOP unavailable" in body
