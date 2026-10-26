"""Loopback-only HTTPS CONNECT relay for Prediction Trader in Docker Desktop.

TLS remains end-to-end. Only public research/package HTTPS targets are allowed.
This works around WSL packets bypassing the Windows VPN/TUN networking stack.
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import select
import socket
import socketserver
import threading
import time

ALLOWED_SUFFIXES = (
    "polymarket.com", "binance.com", "binance.vision", "pypi.org", "pythonhosted.org",
)
ALLOWED_HOSTS = set()
ACTIVE = threading.BoundedSemaphore(64)


def permitted_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return host in ALLOWED_HOSTS or any(host == suffix or host.endswith("." + suffix)
                   for suffix in ALLOWED_SUFFIXES)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not ACTIVE.acquire(blocking=False):
            self.request.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            self.relay()
        except (OSError, ValueError) as exc:
            logging.warning("Relay connection ended: %s", type(exc).__name__)
        finally:
            ACTIVE.release()

    def relay(self):
        self.request.settimeout(15)
        header = b""
        while b"\r\n\r\n" not in header:
            part = self.request.recv(4096)
            if not part:
                return
            header += part
            if len(header) > 16384:
                return
        head, remainder = header.split(b"\r\n\r\n", 1)
        request_line = head.split(b"\r\n", 1)[0].decode("ascii")
        method, target, _ = request_line.split(" ", 2)
        if method == "GET" and target == "/health":
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\nOK",
            )
            return
        if method != "CONNECT":
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        host, port = target.rsplit(":", 1)
        if port != "443" or not permitted_host(host):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            upstream = socket.create_connection((host, 443), timeout=20)
        except OSError:
            logging.warning("Upstream unavailable: %s", host)
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        with upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if remainder:
                upstream.sendall(remainder)
            self.request.settimeout(30)
            upstream.settimeout(30)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.request, upstream], [], [], 120)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    dest = upstream if source is self.request else self.request
                    dest.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18866)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with Server(("127.0.0.1", args.port), Handler) as server:
        logging.info("Research HTTPS relay listening on Windows loopback port %d", args.port)
        server.serve_forever()
