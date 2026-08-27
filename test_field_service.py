import contextlib,json,subprocess,threading,urllib.error,urllib.request
from pathlib import Path
from prepare_field_dashboard import prepare
from serve_dashboard import make_server

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
