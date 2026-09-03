import contextlib,io,json,socket,subprocess,sys,threading,urllib.error,urllib.request
from pathlib import Path
from prepare_field_dashboard import prepare
from serve_dashboard import ReadOnlyServer, make_server

@contextlib.contextmanager
def running(root):
    server=make_server(root,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f"http://127.0.0.1:{server.server_port}"
    finally:server.shutdown();server.server_close();thread.join()

def test_prepare_publication(tmp_path):
    dashboard=tmp_path/"dash";dashboard.mkdir()
    for n in ("index.html","styles.css","app.js"):(dashboard/n).write_text('<script src="app.js"></script>' if n=="index.html" else n)
    quick=tmp_path/"quick";quick.mkdir()
    from prepare_field_dashboard import ROOT_FILES
    for n in ROOT_FILES:(quick/n).write_bytes(b"x")
    telemetry=tmp_path/"t.json";telemetry.write_text("{}")
    result=prepare(dashboard,quick,telemetry,tmp_path/"public")
    assert result["status"]=="PASS" and (tmp_path/"public/quicklook").is_symlink()
    assert "ALMITA_TELEMETRY_ROOT" in (tmp_path/"public/config.js").read_text()

def test_prepare_empty_live_session(tmp_path):
    dashboard=tmp_path/"dash";dashboard.mkdir()
    for n in ("index.html","styles.css","app.js"):(dashboard/n).write_text('<script src="app.js"></script>' if n=="index.html" else n)
    quick=tmp_path/"quick";quick.mkdir();(quick/"quicklook_live_status.json").write_text('{"status":"IDLE"}')
    telemetry=tmp_path/"t.json";telemetry.write_text("{}")
    result=prepare(dashboard,quick,telemetry,tmp_path/"public")
    assert result["status"]=="PASS"
    assert result["publication_state"]=="EMPTY_WAITING_FOR_DERIVATIVES"
    from prepare_field_dashboard import ROOT_FILES
    assert set(result["absent_derivatives"])==set(ROOT_FILES[1:])

def test_http_methods_cache_types_listing_and_traversal(tmp_path):
    (tmp_path/"index.html").write_text("ok");(tmp_path/"x.json").write_text('{}');(tmp_path/"x.png").write_bytes(b'png')
    (tmp_path/"dir").mkdir();(tmp_path/"dir/file").write_text("secret")
    with running(tmp_path) as base:
        with urllib.request.urlopen(base+"/") as r:assert r.status==200 and r.headers.get_content_type()=="text/html"
        req=urllib.request.Request(base+"/x.json",method="HEAD")
        with urllib.request.urlopen(req) as r:assert r.status==200 and "no-cache" in r.headers["Cache-Control"]
        with urllib.request.urlopen(base+"/x.png") as r:assert r.headers.get_content_type()=="image/png"
        for path in ("/missing","/dir/","/../etc/passwd"):
            try:urllib.request.urlopen(base+path);assert False
            except urllib.error.HTTPError as e:assert e.code==404
        for method in ("POST","PUT","DELETE","PATCH"):
            try:urllib.request.urlopen(urllib.request.Request(base+"/",method=method,data=b"x"));assert False
            except urllib.error.HTTPError as e:assert e.code==405

def test_cli_sigterm_graceful_shutdown(tmp_path):
    (tmp_path/"index.html").write_text("ok")
    process=subprocess.Popen(["/home/stellarmate/almita/.venv/bin/python","serve_dashboard.py","--root",str(tmp_path),"--port","0"],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    assert "FIELD STATIC START" in process.stdout.readline()
    process.terminate();assert process.wait(timeout=5)==0
    assert "FIELD STATIC STOP" in process.stdout.read()

def test_end_headers_before_self_path_set_does_not_raise():
    # Reproduces the exact ordering that produced
    # "AttributeError: 'ReadOnlyHandler' object has no attribute 'path'":
    # http.server's parse_request() calls send_error() -> end_headers()
    # for a malformed request line before self.path is ever assigned.
    from serve_dashboard import ReadOnlyHandler
    handler=object.__new__(ReadOnlyHandler)
    handler.request_version="HTTP/1.1"
    handler.wfile=io.BytesIO()
    assert not hasattr(handler,"path")
    handler.end_headers()  # must not raise AttributeError
    assert b"Cache-Control: no-cache\r\n" in handler.wfile.getvalue()

def test_malformed_request_line_gets_complete_error_body_not_cut_off(tmp_path):
    # A bare one-word request line ("GARBAGE") makes http.server's
    # parse_request() call send_error() -> end_headers() while
    # self.request_version is still its "HTTP/0.9" default and self.path
    # was never assigned - the exact ordering that used to raise
    # AttributeError out of end_headers before a single byte of the error
    # body was written, truncating the response and aborting the
    # connection. With the fix the full error body is delivered.
    (tmp_path/"index.html").write_text("ok")
    with running(tmp_path) as base:
        host,port=base.split("//")[1].split(":")
        with socket.create_connection((host,int(port)),timeout=5) as s:
            s.sendall(b"GARBAGE\r\n\r\n")
            s.settimeout(5)
            response=b""
            try:
                while True:
                    chunk=s.recv(4096)
                    if not chunk:break
                    response+=chunk
            except socket.timeout:
                pass
        assert response.startswith(b"<!DOCTYPE HTML>"),response
        assert response.rstrip().endswith(b"</html>"),response
        assert b"400" in response

def test_handle_error_broken_pipe_and_connection_reset_logged_quietly(tmp_path,capsys):
    server=make_server(tmp_path,port=0)
    try:
        for exc_type in (BrokenPipeError,ConnectionResetError):
            try:raise exc_type("simulated client disconnect")
            except exc_type:server.handle_error(None,("127.0.0.1",12345))
    finally:
        server.server_close()
    err=capsys.readouterr().err
    assert "disconnected (BrokenPipeError)" in err
    assert "disconnected (ConnectionResetError)" in err
    assert "Traceback" not in err

def test_handle_error_real_exception_still_gets_full_traceback(tmp_path,capsys):
    server=make_server(tmp_path,port=0)
    try:
        try:raise ValueError("a genuine bug, must not be hidden")
        except ValueError:server.handle_error(None,("127.0.0.1",12345))
    finally:
        server.server_close()
    err=capsys.readouterr().err
    assert "Traceback" in err and "ValueError" in err
