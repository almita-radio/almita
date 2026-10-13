#!/usr/bin/env python3
"""GET/HEAD-only static field server for an already prepared public root."""
import argparse, functools, signal, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
class ReadOnlyHandler(SimpleHTTPRequestHandler):
    def list_directory(self,path):self.send_error(404,"directory listing disabled");return None
    def log_message(self,format,*args):
        # UTC timestamp + component (the default prints local time with no zone)
        from datetime import datetime,timezone
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} INFO  serve_dashboard {self.address_string()} {format%args}",file=sys.stderr,flush=True)
    def _reject(self):self.send_error(405,"read-only service")
    do_POST=_reject;do_PUT=_reject;do_DELETE=_reject;do_PATCH=_reject
    def end_headers(self):
        # http.server calls send_error() - which calls end_headers() - for a
        # malformed request line (e.g. "Bad request syntax") *before*
        # parse_request() reaches the `self.command, self.path = ...` line,
        # so self.path may not exist yet. getattr(...) keeps that case from
        # raising a second exception (AttributeError) out of the error path.
        path=getattr(self,"path","").split("?",1)[0]
        if path.endswith(".json"):self.send_header("Cache-Control","no-cache, max-age=0, must-revalidate")
        elif path.endswith((".js",".css")):self.send_header("Cache-Control","public, max-age=3600")
        else:self.send_header("Cache-Control","no-cache")
        self.send_header("X-Content-Type-Options","nosniff");self.send_header("X-Frame-Options","SAMEORIGIN");self.send_header("Referrer-Policy","same-origin");super().end_headers()
class ReadOnlyServer(ThreadingHTTPServer):
    # A client disappearing mid-request (closed tab, page refresh aborting
    # an in-flight fetch) surfaces here as BrokenPipeError/ConnectionResetError
    # from the request thread - normal on a resident console server, and
    # otherwise logged by the default handle_error() as a full traceback per
    # occurrence. Log those two expected cases as one line; anything else
    # (a real bug) still gets the full traceback via the default handling.
    def handle_error(self,request,client_address):
        exc=sys.exc_info()[1]
        if isinstance(exc,(BrokenPipeError,ConnectionResetError)):
            print(f"[serve_dashboard] client {client_address} disconnected ({exc.__class__.__name__})",file=sys.stderr,flush=True)
            return
        super().handle_error(request,client_address)
def make_server(root,bind="127.0.0.1",port=8088):
    root=Path(root).resolve();handler=functools.partial(ReadOnlyHandler,directory=str(root));return ReadOnlyServer((bind,port),handler)
def main():
    p=argparse.ArgumentParser();p.add_argument("--root",default="data/field_web");p.add_argument("--bind",default="127.0.0.1");p.add_argument("--port",type=int,default=8088);a=p.parse_args();server=make_server(a.root,a.bind,a.port)
    def stop(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    print(f"FIELD STATIC START http://{a.bind}:{server.server_port} root={Path(a.root).resolve()}",flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();print("FIELD STATIC STOP",flush=True)
if __name__=="__main__":main()
