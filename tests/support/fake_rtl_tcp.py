"""A stand-in rtl_tcp server for stop tests: the 12-byte RTL0 header, then an endless uint8 I/Q stream at the sample
rate the client asks for (command 0x02), accepting every 5-byte command. No hardware.

    python tests/support/fake_rtl_tcp.py --port 0     -> prints "LISTENING <port>"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys


async def client(reader, writer):
    rate = 250_000
    writer.write(b"RTL0" + struct.pack(">II", 5, 29))
    await writer.drain()

    async def commands():
        nonlocal rate
        try:
            while True:
                data = await reader.readexactly(5)
                cmd, value = data[0], struct.unpack(">I", data[1:])[0]
                if cmd == 0x02 and value > 0:
                    rate = value
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
    task = asyncio.create_task(commands())
    try:
        loop = asyncio.get_running_loop()
        tick = 0.02
        owed, last = 0.0, loop.time()
        while not task.done():
            await asyncio.sleep(tick)
            now = loop.time()
            owed += (now - last) * rate * 2
            last = now
            n = int(owed)
            if n > 0:
                owed -= n
                writer.write(os.urandom(min(n, 2_000_000)).translate(bytes([120 + (i % 16) for i in range(256)])))
                await writer.drain()
    except (ConnectionError, RuntimeError):
        pass
    finally:
        task.cancel()
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def serve(port: int) -> None:
    server = await asyncio.start_server(client, "127.0.0.1", port)
    print(f"LISTENING {server.sockets[0].getsockname()[1]}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=0)
    try:
        asyncio.run(serve(p.parse_args().port))
    except KeyboardInterrupt:
        sys.exit(0)
