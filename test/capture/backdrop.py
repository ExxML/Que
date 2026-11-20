"""Serve an isolated, nonce-bound capture fixture without any account or artifact routes."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# The local-share recorder page must not repaint the fixture or share its window title.
RECORDER = (
    b'<!doctype html><meta charset="utf-8"><title>Que capture audit recorder</title>'
)


class Backdrop:
    """Own the local visual source and advance its measurement phases."""

    def __init__(self, nonce: int) -> None:
        self.state = {"nonce": nonce, "phase": 0}
        self.presented: dict[str, int] = {}
        self.condition = threading.Condition()
        state = self.state
        presented = self.presented
        condition = self.condition
        content = Path(__file__).with_name("backdrop.html").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            """Serve only the static fixture and its public visual phase."""

            def do_GET(self) -> None:
                """Return a bounded fixture resource."""
                if self.path not in ("/", "/?tab=1", "/state", "/recorder"):
                    self.send_error(404)
                    return
                payload = (
                    json.dumps(state).encode()
                    if self.path == "/state"
                    else RECORDER
                    if self.path == "/recorder"
                    else content
                )
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/json" if self.path == "/state" else "text/html",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: Any) -> None:
                """Keep the fixture server out of the command protocol."""

            def do_POST(self) -> None:
                """Acknowledge a painted source phase without accepting arbitrary state changes."""
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/presented" or not 0 < length <= 128:
                    self.send_error(400)
                    return
                try:
                    value = json.loads(self.rfile.read(length))
                    if value["nonce"] != state["nonce"] or value["client"] not in (
                        "desktop",
                        "tab",
                    ):
                        raise ValueError("Wrong fixture identity.")
                    with condition:
                        presented[value["client"]] = int(value["phase"])
                        condition.notify_all()
                except (ValueError, KeyError, TypeError):
                    self.send_error(400)
                    return
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """Return the loopback fixture URL."""
        return f"http://127.0.0.1:{self.server.server_port}/"

    def start(self) -> None:
        """Start serving the fixture."""
        self.thread.start()

    def phase(self, value: int) -> None:
        """Advance the visible phase without resetting the liveness counter."""
        self.state["phase"] = value

    def wait_presented(self, source: str) -> None:
        """Wait for the selected source to paint its marker before triggering a Que action."""
        client = "tab" if source == "tab" else "desktop"
        with self.condition:
            if not self.condition.wait_for(
                lambda: self.presented.get(client) == self.state["phase"], timeout=10
            ):
                raise TimeoutError(
                    "The shared fixture did not acknowledge its measurement phase."
                )

    def close(self) -> None:
        """Stop the owned listener and wait for its worker."""
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
