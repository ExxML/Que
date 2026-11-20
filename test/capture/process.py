"""Supervise the disposable Qt fixture without adding a control endpoint to Que."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from stealth.isolation import child_environment


class FixtureProcess:
    """Own an isolated source process and correlate Qt action acknowledgements."""

    def __init__(self, directory: Path) -> None:
        environment = child_environment(directory)
        environment["QT_QPA_PLATFORM"] = "windows"
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("fixture.py")),
                "--source",
                str(directory / "isolated"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            cwd=directory / "isolated",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.replies: queue.Queue[dict[str, Any]] = queue.Queue()
        self.sequence = 0
        self.log: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.threads = [
            threading.Thread(target=self._read, daemon=True),
            threading.Thread(target=self._errors, daemon=True),
        ]
        for thread in self.threads:
            thread.start()
        try:
            self.ready = self.replies.get(timeout=45)
            if not self.ready.get("ready"):
                raise RuntimeError("Fixture did not initialize.")
        except Exception:
            self.close()
            raise

    def command(self, command: str, **values: Any) -> dict[str, Any]:
        """Wait for the exact action acknowledgement or fail on process loss."""
        self.sequence += 1
        request = {"id": self.sequence, "command": command, **values}
        if self.process.poll() is not None:
            raise RuntimeError("Fixture exited before the action.")
        assert self.process.stdin is not None
        self.process.stdin.write(f"{json.dumps(request)}\n")
        self.process.stdin.flush()
        result = self.replies.get(timeout=30)
        self.log.append({"request": request, "response": result})
        if result.get("id") != self.sequence or not result.get("ok"):
            raise RuntimeError(f"Fixture action failed: {result}")
        return result

    def close(self) -> None:
        """Request clean exit and terminate only this child if it stops responding."""
        if self.process.poll() is None:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.write('{"command":"quit"}\n')
                    self.process.stdin.flush()
                except OSError:
                    pass
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=8)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        for thread in self.threads:
            thread.join(timeout=2)

    def _read(self) -> None:
        """Read action replies without processing Qt work in this thread."""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            if line.startswith("CAPTURE:"):
                self.replies.put(json.loads(line[len("CAPTURE:") :]))

    def _errors(self) -> None:
        """Retain fixture diagnostics privately for failed actions."""
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.errors.append(line.rstrip())
