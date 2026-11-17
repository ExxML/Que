"""Serve the manual stealth test site on loopback only.

Run `python test/manual_site/serve.py`, then open the printed address in the browser under test.
The server is read-only: it serves `index.html` plus two timing endpoints the page uses
to look for effects of Que on network timing.
"""

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

SITE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
TICK_LIMIT_SECONDS = 180.0


class SiteHandler(BaseHTTPRequestHandler):
    """Serve the test page, a ping endpoint and a steady tick stream."""

    protocol_version = "HTTP/1.1"
    server_version = "QueStealthLab"

    def do_GET(self) -> None:
        """Route a GET request to the page or one of the timing endpoints."""
        path, _, query = self.path.partition("?")
        if not self._host_allowed():
            self.send_error(403)
        elif path in ("/", "/index.html"):
            self._send_bytes(
                (SITE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8"
            )
        elif path == "/ping":
            self._send_bytes(
                json.dumps({"t": time.time()}).encode(), "application/json"
            )
        elif path == "/ticks":
            self._stream_ticks(query)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        """Stay silent so request logging never competes with the timing tests."""

    def _host_allowed(self) -> bool:
        """Return whether the Host header names this loopback server (blocks DNS rebinding).

        Returns:
            bool: True when the request addressed localhost or 127.0.0.1.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1")

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        """Send a complete uncached response.

        Args:
            body (bytes): The response body.
            content_type (str): The Content-Type header value.
        """
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_ticks(self, query: str) -> None:
        """Stream one timestamp line every interval until the client leaves.

        Args:
            query (str): The URL query string; `ms` sets the interval (10 to 1000, default 50).
        """
        try:
            interval = (
                min(1000.0, max(10.0, float(parse_qs(query).get("ms", ["50"])[0])))
                / 1000.0
            )
        except ValueError:
            interval = 0.05
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        start = time.perf_counter()
        count = 0
        try:
            while time.perf_counter() - start < TICK_LIMIT_SECONDS:
                count += 1
                delay = start + count * interval - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                self.wfile.write(f"{time.time():.6f}\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass


def main() -> None:
    """Parse arguments, start the server and optionally open the browser."""
    parser = argparse.ArgumentParser(
        description="Serve the Que manual stealth test site."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port to listen on."
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open the default browser."
    )
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), SiteHandler)
    except OSError as error:
        raise SystemExit(
            f"Cannot listen on port {args.port}: {error}. Try --port."
        ) from error
    address = f"http://localhost:{args.port}/"
    print(f"Que Stealth Lab: {address}  (Ctrl+C to stop)")
    print(
        "Open it in the browser you want to test. Use a fresh window or profile for realistic results."
    )
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
