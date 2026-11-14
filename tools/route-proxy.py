"""An HTTPS proxy (HTTP CONNECT) that makes a second route for the SMTranscript worker.

It runs on another machine (first: the Hetzner server mp-streamfab-test) and
lets exactly one caller in - the worker's own address - with a user and
password (Proxy-Authorization: Basic). Every connection it opens leaves
through one fixed local address (`source`), so the platform sees that
machine's own IP even while a VPN holds the default route. CONNECT to port 443
only, no private addresses, a ceiling on connections. HTTP CONNECT rather than
SOCKS because every part of the worker (yt-dlp, Instaloader, urllib) speaks it
without extra packages.

Settings: route-proxy.json next to this file - listen, port, user, password,
allow (one IP), source (local IP to leave from), max_connections, log.
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import socket
import time
from pathlib import Path

CONF = json.loads(Path(__file__).with_suffix(".json").read_text())
LISTEN = CONF.get("listen", "0.0.0.0")
PORT = int(CONF.get("port", 1080))
EXPECTED = ("Basic " + base64.b64encode(f"{CONF['user']}:{CONF['password']}".encode()).decode()).encode()
ALLOW = CONF["allow"]
SOURCE = CONF["source"]
MAX = int(CONF.get("max_connections", 64))
LOG = Path(CONF.get("log") or Path(__file__).with_name("route-proxy.log"))
CRLF2 = b"\r\n\r\n"


def log(line: str) -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 5_000_000:
            LOG.replace(LOG.with_suffix(".old"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + line + "\n")
    except OSError:
        pass


def status(code: int, reason: str, extra: str = "") -> bytes:
    return f"HTTP/1.1 {code} {reason}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode()


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def answer(writer, data: bytes):
    writer.write(data)
    await writer.drain()
    writer.close()


async def serve(reader, writer):
    head = await asyncio.wait_for(reader.readuntil(CRLF2), 15)
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split()
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if ":" in l)}
    if not hmac.compare_digest(headers.get("proxy-authorization", "").encode(), EXPECTED):
        log("refused: wrong or missing credentials")
        return await answer(writer, status(407, "Proxy Authentication Required", 'Proxy-Authenticate: Basic realm="smt"\r\n'))
    if len(parts) != 3 or parts[0] != "CONNECT":
        return await answer(writer, status(405, "Method Not Allowed"))
    host, _, port = parts[1].rpartition(":")
    host = host.strip("[]")
    if port != "443":
        return await answer(writer, status(403, "Forbidden"))
    try:
        if ipaddress.ip_address(host).is_private:
            return await answer(writer, status(403, "Forbidden"))
    except ValueError:
        pass
    try:
        out_r, out_w = await asyncio.wait_for(
            asyncio.open_connection(host, 443, family=socket.AF_INET, local_addr=(SOURCE, 0)), 20)
    except Exception as exc:
        log(f"connect failed {host} {type(exc).__name__}")
        return await answer(writer, status(502, "Bad Gateway"))
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, out_w), pipe(out_r, writer))


async def main():
    slots = asyncio.Semaphore(MAX)

    async def handle(reader, writer):
        if writer.get_extra_info("peername")[0] != ALLOW:
            writer.close()
            return
        async with slots:
            try:
                await serve(reader, writer)
            except Exception as exc:  # one broken connection never stops the others
                log(f"error {type(exc).__name__}")
                writer.close()

    server = await asyncio.start_server(handle, LISTEN, PORT)
    log(f"listening on {LISTEN}:{PORT}, only {ALLOW}, leaving from {SOURCE}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
