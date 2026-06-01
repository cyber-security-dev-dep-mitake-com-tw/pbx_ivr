#!/usr/bin/env python3
"""
Transparent TCP proxy: host:18443 → 192.168.100.100:443 (HT812 HTTPS)

Allows Docker containers to reach the HT812 on the direct-LAN
192.168.2.x subnet, which is only accessible from the Mac host.

  Docker container  →  host.docker.internal:18443  →  192.168.100.100:443

Usage:
  python scripts/ht812_proxy.py          # foreground
  python scripts/ht812_proxy.py &        # background

Stop:
  kill %1   (if backgrounded in same shell)
  pkill -f ht812_proxy.py

The proxy forwards raw TCP bytes in both directions (SSL passthrough).
The Docker container terminates TLS directly with the HT812 device;
the proxy never sees decrypted content.
"""

import asyncio
import sys

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 18443
TARGET_HOST = "192.168.100.100"
TARGET_PORT = 443
BUF = 65536


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(BUF):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    peer = client_w.get_extra_info("peername")
    try:
        target_r, target_w = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError as exc:
        print(f"  Cannot reach HT812 at {TARGET_HOST}:{TARGET_PORT} — {exc}", flush=True)
        client_w.close()
        return

    print(f"  tunnel {peer} → {TARGET_HOST}:{TARGET_PORT}", flush=True)
    await asyncio.gather(_pipe(client_r, target_w), _pipe(target_r, client_w))


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    addr = server.sockets[0].getsockname()
    print(f"HT812 proxy listening on {addr[0]}:{addr[1]}")
    print(f"  → forwarding to {TARGET_HOST}:{TARGET_PORT}")
    print(f"  Set HT812_HOST=https://host.docker.internal:{LISTEN_PORT} in .env")
    print(f"  Ctrl-C to stop\n", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProxy stopped.")
        sys.exit(0)
