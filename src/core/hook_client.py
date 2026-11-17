"""Connect Que to its input helper process and turn the helper's decisions into Qt signals."""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.hook_host import AUTH_ENVIRONMENT

CREATE_NO_WINDOW = 0x08000000
STARTUP_SECONDS = 15.0  # A source checkout imports Qt in the helper before it connects
SILENCE_SECONDS = 3.0
WATCHDOG_SECONDS = 1.0


def hook_host_command(address: str) -> list[str]:
    """Build the command that starts the input helper from this build of Que.

    Args:
        address (str): The named pipe the helper connects to.

    Returns:
        list[str]: The helper's command line.
    """
    if "__compiled__" in globals():  # Nuitka: the executable itself hosts the helper
        return [sys.executable, "--hook-host", address]
    script = Path(__file__).resolve().parent.parent / "que.py"
    return [sys.executable, str(script), "--hook-host", address]


class HookClient(QObject):
    """Own the input helper's lifetime and the state it needs to make decisions.

    Every protection failure is reported through `failed`, and failures that mean
    input may already have reached another application through `alert`.
    """

    action = pyqtSignal(str)
    key_input = pyqtSignal(int, int, int, bool, bool, object)
    pointer_input = pyqtSignal(int, int, int, int, int)
    armed_changed = pyqtSignal(bool)
    lock_toggled = pyqtSignal(int)
    quit_ready = pyqtSignal()
    failed = pyqtSignal(str)
    alert = pyqtSignal(str)
    received = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.outbox: Callable[[dict[str, Any]], None] = self._send_to_host
        self.state: dict[str, dict[str, Any]] = {}
        self.held_modifiers: tuple[int, ...] = ()
        self.last_press_seq = 0
        self.connected = False
        self.started = False
        self.stopping = False
        self.generation = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.commands: Connection | None = None
        self.send_lock = threading.Lock()
        self.last_received = (
            0.0  # Written by the pipe thread, so a busy UI thread is not "silence"
        )
        self.last_check = 0.0
        self.deadline = 0.0
        self.received.connect(self.dispatch)
        self.watchdog = QTimer(self)
        self.watchdog.setInterval(int(WATCHDOG_SECONDS * 1000))
        self.watchdog.timeout.connect(self._check_helper)

    def start(self) -> None:
        """Start the input helper and supervise it until `stop`."""
        self.started = True
        self.stopping = False
        self.last_check = time.monotonic()
        self._spawn()
        self.watchdog.start()

    def stop(self) -> None:
        """Disconnect; the helper releases its hooks once every owned press is released."""
        self.stopping = True
        self.watchdog.stop()
        self.connected = False
        with self.send_lock:
            if self.commands is not None:
                self.commands.close()
                self.commands = None

    def set_visible(self, visible: bool) -> None:
        """Tell the helper whether window-only shortcuts are active.

        Args:
            visible (bool): Whether Que's main window is visible.
        """
        self._set_state({"type": "visible", "value": visible})

    def set_routing(self, enabled: bool) -> None:
        """Tell the helper whether clicks over Que belong to Que.

        Args:
            enabled (bool): Whether pointer input over Que is routed to Que.
        """
        self._set_state({"type": "routing", "value": enabled})

    def set_regions(self, regions: list[tuple[int, int, int, int, int]]) -> None:
        """Publish Que's window rectangles in front-to-back order.

        Args:
            regions (list[tuple[int, int, int, int, int]]): Handle and physical rectangle per window.
        """
        message = {"type": "regions", "value": [list(region) for region in regions]}
        if self.state.get("regions") != message:
            self._set_state(message)

    def send_armed(self, armed: bool) -> None:
        """Tell the helper whether Que's input is in typing mode after the latest press.

        Args:
            armed (bool): Whether ordinary keys now belong to Que.
        """
        self._set_state({"type": "armed", "value": armed, "seq": self.last_press_seq})

    def request_quit(self) -> bool:
        """Ask the helper to signal when every owned press is released.

        Returns:
            bool: Whether the helper will answer; otherwise quit immediately.
        """
        if not self.connected:
            return False
        self.outbox({"type": "quit"})
        return True

    def dispatch(self, message: dict[str, Any]) -> None:
        """Handle one helper event on the Qt thread.

        Args:
            message (dict[str, Any]): The event; its "type" names what happened.
        """
        kind = message.get("type")
        if kind == "key":
            self.held_modifiers = tuple(message["held"])
            self.key_input.emit(
                message["vk"],
                message["scan"],
                message["flags"],
                message["down"],
                message["repeated"],
                self.held_modifiers,
            )
        elif kind == "pointer":
            self.held_modifiers = tuple(message["held"])
            self.last_press_seq = max(self.last_press_seq, int(message["seq"]))
            self.pointer_input.emit(
                message["message"],
                message["x"],
                message["y"],
                message["data"],
                message["hwnd"],
            )
        elif kind == "lock":
            self.lock_toggled.emit(int(message["vk"]))
        elif kind == "armed":
            self.last_press_seq = max(self.last_press_seq, int(message["seq"]))
            self.armed_changed.emit(bool(message["value"]))
        elif kind == "action":
            self.action.emit(str(message["name"]))
        elif kind == "quit_ready":
            self.quit_ready.emit()
        elif kind == "error":
            self.failed.emit(str(message["text"]))
        elif kind == "leak":
            events = ", ".join(message["events"])
            restored = (
                "Protection was restored."
                if message["restored"]
                else "Protection could NOT be restored; keys and clicks are not hidden."
            )
            self.alert.emit(
                f"Windows let input past Que's hooks, so it reached the active app: {events}. "
                f"{restored}"
            )
        elif kind == "ready":
            self.connected = True
        elif (
            kind == "disconnected"
            and int(message["generation"]) == self.generation
            and not self.stopping
        ):
            self.connected = False
            self.alert.emit(
                "Que's input helper stopped, so keys and clicks are not hidden. Restarting it."
            )
            self._spawn()

    def _set_state(self, message: dict[str, Any]) -> None:
        """Remember and send the latest value of one kind of state.

        Args:
            message (dict[str, Any]): The state update.
        """
        self.state[str(message["type"])] = message
        self.outbox(message)

    def _send_to_host(self, message: dict[str, Any]) -> None:
        """Send a command if connected; state is re-sent after (re)connection anyway.

        Args:
            message (dict[str, Any]): The command.
        """
        with self.send_lock:
            if self.commands is None:
                return
            try:
                self.commands.send_bytes(json.dumps(message).encode("utf-8"))
            except OSError:
                self.commands = None

    def _spawn(self) -> None:
        """Start a fresh helper process and connect to it in the background."""
        self.generation += 1
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
        self.process = None
        self.commands = None
        self.connected = False
        address = rf"\\.\pipe\que-input-{secrets.token_hex(16)}"
        authkey = secrets.token_bytes(32)
        try:
            listener = Listener(address, family="AF_PIPE", authkey=authkey)
            self.process = subprocess.Popen(
                hook_host_command(address),
                env={**os.environ, AUTH_ENVIRONMENT: authkey.hex()},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as error:
            self.failed.emit(f"Que's input helper could not start: {error}")
            self.deadline = time.monotonic() + STARTUP_SECONDS
            return
        self.deadline = time.monotonic() + STARTUP_SECONDS
        threading.Thread(
            target=self._connect, args=(listener, self.generation), daemon=True
        ).start()

    def _connect(self, listener: Listener, generation: int) -> None:
        """Accept the helper's two pipes, send the current state, and relay its events.

        Args:
            listener (Listener): The authenticated pipe listener.
            generation (int): The helper instance this thread serves.
        """
        try:
            commands = listener.accept()
            events = listener.accept()
        except (OSError, EOFError):
            return
        finally:
            listener.close()
        with self.send_lock:
            if generation != self.generation:
                commands.close()
                events.close()
                return
            self.commands = commands
            for message in list(self.state.values()):
                commands.send_bytes(json.dumps(message).encode("utf-8"))
        try:
            while True:
                message = json.loads(events.recv_bytes())
                self.last_received = time.monotonic()
                self.received.emit(message)
        except (EOFError, OSError, ValueError):
            pass
        self.received.emit({"type": "disconnected", "generation": generation})

    def _check_helper(self) -> None:
        """Restart a helper that stopped responding, telling the user input may be exposed."""
        if self.stopping:
            return
        now = time.monotonic()
        if now - self.last_check > 2 * WATCHDOG_SECONDS:
            # Que itself was suspended (sleep, debugger) or blocked; give the helper time to resume
            self.last_received = now
            self.deadline = max(self.deadline, now + SILENCE_SECONDS)
        self.last_check = now
        silent = (
            now - self.last_received > SILENCE_SECONDS
            if self.connected
            else now > self.deadline
        )
        if silent:
            self.alert.emit(
                "Que's input helper stopped responding, so keys and clicks may not be "
                "hidden. Restarting it."
            )
            self._spawn()
