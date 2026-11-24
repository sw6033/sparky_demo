"""Bounded keep-alive connection pooling + streaming HTTP response & stdlib WebSocket."""
from __future__ import annotations

import base64
import http.client
import io
import os
import select
import socket
import ssl
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib import error, parse, request


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and not ctx.get_ca_certs():
        cert = Path("/etc/ssl/cert.pem")
        if cert.is_file():
            ctx.load_verify_locations(cafile=str(cert))
    return ctx


class IdlePool:
    def __init__(self, limit: int = 4, ttl: float = 45.0):
        self.limit = limit
        self.ttl = ttl
        self.idle: list[tuple[object, float, object]] = []
        self.lock = threading.Lock()

    def take(self, key: object):
        now = time.monotonic()
        chosen = None
        with self.lock:
            kept = []
            for saved, stamp, conn in self.idle:
                if now - stamp > self.ttl:
                    try:
                        conn.close()
                    except Exception:
                        pass
                elif saved == key and chosen is None:
                    chosen = conn
                else:
                    kept.append((saved, stamp, conn))
            self.idle = kept
        return chosen

    def put(self, key: object, conn: object) -> None:
        with self.lock:
            self.idle.append((key, time.monotonic(), conn))
            if len(self.idle) > self.limit:
                old = self.idle.pop(0)[2]
                try:
                    old.close()
                except Exception:
                    pass

    def clear(self) -> None:
        with self.lock:
            for _, _, conn in self.idle:
                try:
                    conn.close()
                except Exception:
                    pass
            self.idle = []

    def available(self, key: object) -> int:
        now = time.monotonic()
        with self.lock:
            return sum(saved == key and now - stamp <= self.ttl for saved, stamp, _ in self.idle)


http_pool = IdlePool(limit=6, ttl=45.0)
speech_pool = IdlePool(limit=4, ttl=90.0)


@contextmanager
def open_response(req: request.Request, timeout: float = 30.0, context: ssl.SSLContext | None = None):
    """Yield a live streaming HTTPResponse, pooling keep-alive HTTPS connections to api.openai.com."""
    ctx = context or tls_context()
    url = parse.urlsplit(req.full_url)
    if url.scheme != "https" or url.netloc != "api.openai.com":
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            yield resp
        return

    proxy_url = request.getproxies().get("https", "")
    proxy = parse.urlsplit(proxy_url if "://" in proxy_url else "//" + proxy_url)
    if proxy_url and (proxy.scheme not in ("", "http") or proxy.username or proxy.password):
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            yield resp
        return
    if request.proxy_bypass(url.hostname):
        proxy_url = ""

    key = (url.netloc, proxy_url, req.get_header("Authorization"))
    conn = http_pool.take(key)
    if conn and (not conn.sock or select.select([conn.sock], [], [], 0)[0]):
        conn.close()
        conn = None
    if conn is None:
        if proxy_url:
            conn = http.client.HTTPSConnection(proxy.hostname, proxy.port or 80, timeout=timeout, context=ctx)
            conn.set_tunnel(url.hostname, 443)
        else:
            conn = http.client.HTTPSConnection(url.netloc, timeout=timeout, context=ctx)
    else:
        conn.timeout = timeout
        conn.sock.settimeout(timeout)

    reusable = False
    try:
        path = parse.urlunsplit(("", "", url.path, url.query, ""))
        conn.request(req.get_method(), path, body=req.data, headers=dict(req.header_items()))
        resp = conn.getresponse()
        if resp.status >= 400:
            err_body = resp.read(65536)
            if not resp.will_close:
                http_pool.put(key, conn)
                conn = None
            raise error.HTTPError(req.full_url, resp.status, "Upstream request failed", resp.headers, io.BytesIO(err_body))
        yield resp
        # Return connection to pool only if response stream was fully consumed cleanly
        if not resp.will_close and resp.isclosed():
            reusable = True
    except http.client.HTTPException as exc:
        raise OSError("Upstream connection failed") from exc
    finally:
        if conn is not None:
            if reusable:
                http_pool.put(key, conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass


def urlopen(req: request.Request, timeout: float = 30.0, context: ssl.SSLContext | None = None, max_bytes: int = 262144) -> io.BytesIO:
    with open_response(req, timeout=timeout, context=context) as resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Upstream response too large")
        resp.close()
        return io.BytesIO(data)


class StdlibWebSocket:
    """Minimal RFC-6455 TLS WebSocket client so zero external pip packages are required."""

    def __init__(self, sock: ssl.SSLSocket):
        self.sock = sock

    @classmethod
    def connect(cls, wss_url: str, headers: dict[str, str], timeout: float = 15.0, context: ssl.SSLContext | None = None) -> "StdlibWebSocket":
        parsed = parse.urlsplit(wss_url)
        host = parsed.hostname or "api.openai.com"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))

        proxy_url = "" if request.proxy_bypass(host) else request.getproxies().get("https", "")
        raw_sock: socket.socket
        if proxy_url:
            p = parse.urlsplit(proxy_url if "://" in proxy_url else "//" + proxy_url)
            raw_sock = socket.create_connection((p.hostname, p.port or 80), timeout=timeout)
            connect_req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii")
            raw_sock.sendall(connect_req)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                chunk = raw_sock.recv(1024)
                if not chunk:
                    raw_sock.close()
                    raise ConnectionError("Proxy closed during CONNECT")
                buf += chunk
            if not buf.startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                raw_sock.close()
                raise ConnectionError("Proxy CONNECT failed")
        else:
            raw_sock = socket.create_connection((host, port), timeout=timeout)

        ctx = context or tls_context()
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host) if parsed.scheme == "wss" else raw_sock
        tls_sock.settimeout(timeout)

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in headers.items():
            req_lines.append(f"{k}: {v}")
        tls_sock.sendall(("\r\n".join(req_lines) + "\r\n\r\n").encode("utf-8"))

        resp_buf = b""
        while b"\r\n\r\n" not in resp_buf and len(resp_buf) < 16384:
            chunk = tls_sock.recv(2048)
            if not chunk:
                tls_sock.close()
                raise ConnectionError("WebSocket handshake EOF")
            resp_buf += chunk
        status_line = resp_buf.split(b"\r\n", 1)[0]
        if b"101" not in status_line:
            tls_sock.close()
            raise ConnectionError(f"WebSocket upgrade rejected: {status_line.decode('latin1', 'ignore')}")
        leftover = resp_buf.split(b"\r\n\r\n", 1)[1]
        ws = cls(tls_sock)
        ws._buf = bytearray(leftover)
        return ws

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionResetError("WebSocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        header = bytes([0x80 | opcode, 0x80 | min(125, len(payload))]) + mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload[:125]))
        self.sock.sendall(header + masked)

    def recv(self) -> str:
        fragments: list[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > 16 * 1024 * 1024:
                raise ValueError("WebSocket frame too large")
            mask_key = self._read_exact(4) if masked else b""
            data = self._read_exact(length)
            if masked:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionResetError("WebSocket closed by peer")
            if opcode == 0x9:
                self._send_control(0xA, data)
                continue
            if opcode == 0xA:
                continue
            fragments.append(data)
            if fin:
                return b"".join(fragments).decode("utf-8")

    def close(self) -> None:
        try:
            self._send_control(0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
