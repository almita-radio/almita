#!/usr/bin/env python3
"""GET/HEAD-only static field server for an already prepared public root."""
import argparse, functools, signal
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
class ReadOnlyHandler(SimpleHTTPRequestHandler):
    def list_directory(self,path):self.send_error(404,"directory listing disabled");return None
    def _reject(self):self.send_error(405,"read-only service")
    do_POST=_reject;do_PUT=_reject;do_DELETE=_reject;do_PATCH=_reject
    def end_headers(self):
        path=self.path.split("?",1)[0]
        if path.endswith(".json"):self.send_header("Cache-Control","no-cache, max-age=0, must-revalidate")
        elif path.endswith((".js",".css")):self.send_header("Cache-Control","public, max-age=3600")
        else:self.send_header("Cache-Control","no-cache")
        self.send_header("X-Content-Type-Options","nosniff");super().end_headers()
def make_server(root,bind="127.0.0.1",port=8088):
    root=Path(root).resolve();handler=functools.partial(ReadOnlyHandler,directory=str(root));return ThreadingHTTPServer((bind,port),handler)
def main():
    p=argparse.ArgumentParser();p.add_argument("--root",default="data/field_web");p.add_argument("--bind",default="127.0.0.1");p.add_argument("--port",type=int,default=8088);a=p.parse_args();server=make_server(a.root,a.bind,a.port)
    def stop(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    print(f"FIELD STATIC START http://{a.bind}:{server.server_port} root={Path(a.root).resolve()}",flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();print("FIELD STATIC STOP",flush=True)
if __name__=="__main__":main()
