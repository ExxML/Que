"""Offer a read-only owned endpoint for a separately permissioned HTTPS origin."""

import argparse
import base64
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class OwnedServer(ThreadingHTTPServer):
    """Expose only a known control marker, never application or native evidence."""

    def __init__(self, origin: str, output: Path) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("Supply the exact permissioned HTTPS origin.")
        self.allowed_origin = f"https://{parsed.netloc}"
        self.output = output
        super().__init__(("127.0.0.1", 0), OwnedHandler)


class OwnedHandler(BaseHTTPRequestHandler):
    """Serve a CORS control and WebSocket marker to the declared origin only."""

    server: OwnedServer

    def do_GET(self) -> None:
        """Record control requests and return only synthetic endpoint-owned content."""
        if (
            self.path != "/control"
            or self.headers.get("Origin") != self.server.allowed_origin
            or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
        ):
            self.send_error(403)
            return
        record = {
            "wall_ms": time.time_ns() // 1_000_000,
            "origin": self.headers.get("Origin"),
            "upgrade": self.headers.get("Upgrade"),
            "control": "QUE-AUDIT-OWNED-ENDPOINT",
        }
        with self.server.output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers.get("Sec-WebSocket-Key", "")
            try:
                if len(base64.b64decode(key, validate=True)) != 16:
                    raise ValueError("Invalid WebSocket key.")
            except ValueError:
                self.send_error(400)
                return
            accept = base64.b64encode(
                hashlib.sha1(
                    f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()
                ).digest()
            ).decode()
            self.protocol_version = "HTTP/1.1"
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            message = b"QUE-AUDIT-OWNED-ENDPOINT"
            self.wfile.write(bytes([0x81, len(message)]) + message + b"\x88\x00")
            self.close_connection = True
            return
        payload = json.dumps(record).encode()
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", self.server.allowed_origin)
        self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep console logs free of browser headers."""


def main() -> None:
    """Run the synthetic endpoint without publishing or modifying any external site."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with OwnedServer(args.origin, args.output) as server:
        print(
            f"Owned target: http://127.0.0.1:{server.server_port}/control", flush=True
        )
        print(
            "Host test/network-origin-test.html on the permissioned origin; preserve normal browser security and grant permissions through browser UI.",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
