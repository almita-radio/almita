"""ALMITA instrument forwarder - runs on the notebook.

Listens on 127.0.0.1:1234 (rtl_tcp) and 127.0.0.1:7624 (INDI) and carries every connection to the same port of the
capture Pi named in ~/.config/almita/instrument.json (instrument_host). The host is read again for every new
connection, so a change made in the web's STATUS page applies to the next connection without a restart; a
connection already open keeps its Pi until it closes.

Never run it on the machine that serves rtl_tcp/INDI itself (the Pi 5 setup): the ports would collide - with no
host configured it refuses every connection instead of looping back to itself.

Usage: python instrument_forwarder.py [--listen 127.0.0.1]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Optional, Sequence

import instrument_host

CHUNK = 256 * 1024


def _log(msg: str) -> None:
    sys.stderr.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")
    sys.stderr.flush()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _handler(port: int):
    async def handle(c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        host = instrument_host.remote_host()
        if host is None:
            _log(f":{port} refused - no capture host configured in {instrument_host.instrument_file()}")
            c_writer.close()
            return
        try:
            r_reader, r_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5.0)
        except (OSError, asyncio.TimeoutError) as exc:
            _log(f":{port} -> {host}:{port} FAILED: {type(exc).__name__}: {exc}")
            c_writer.close()
            return
        for w in (c_writer, r_writer):
            sock = w.get_extra_info("socket")
            if sock is not None:
                import socket
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _log(f":{port} -> {host}:{port} open")
        await asyncio.gather(_pipe(c_reader, r_writer), _pipe(r_reader, c_writer))
        _log(f":{port} -> {host}:{port} closed")
    return handle


async def serve(listen: str, ports: Sequence[int]) -> None:
    servers = [await asyncio.start_server(_handler(p), listen, p) for p in ports]
    _log(f"forwarding {', '.join(f'{listen}:{p}' for p in ports)} -> {instrument_host.where()} (re-read per connection)")
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", default="127.0.0.1")
    a = ap.parse_args(argv)
    try:
        asyncio.run(serve(a.listen, (instrument_host.RTL_TCP_PORT, instrument_host.INDI_PORT)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
