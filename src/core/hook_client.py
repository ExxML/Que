"""Connect Que to its input helper process and turn the helper's decisions into Qt signals."""

import contextlib
import ctypes
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from multiprocessing.connection import Connection, Listener, PipeConnection
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.hook_host import AUTH_ENVIRONMENT

CREATE_NO_WINDOW = 0x08000000
STARTUP_SECONDS = 15.0  # Generous, so a slow cold start (antivirus scan) is never reported as a failure
SILENCE_SECONDS = 3.0
WATCHDOG_SECONDS = 1.0
ELEVATION_DECLINED = (
    "Que's input helper is not running as administrator, so Que's shortcuts don't work "
    "and keys aren't hidden while an administrator app such as Task Manager is focused."
)
STILL_ACTIVE = 259
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_HIDE = 0


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

    def __init__(self, elevate: bool = False) -> None:
        super().__init__()
        # Windows hides input to administrator windows from hooks of unelevated processes
        self.elevate = elevate and not ctypes.windll.shell32.IsUserAnAdmin()
        self.admin_notice = (
            self.elevate
        )  # Explain the limit once a helper runs unelevated
        self.outbox: Callable[[dict[str, Any]], None] = self._send_to_host
        self.state: dict[str, dict[str, Any]] = {}
        self.held_modifiers: tuple[int, ...] = ()
        self.last_press_seq = 0
        self.connected = False
        self.started = False
        self.stopping = False
        self.generation = 0
        self.process: subprocess.Popen[bytes] | ElevatedProcess | None = None
        self.commands: Connection | PipeConnection | None = None
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
            kind == "elevation_declined"
            and int(message["generation"]) == self.generation
            and not self.stopping
        ):
            self._spawn()
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
        with self.send_lock:
            if self.commands is not None:
                # Windows may refuse to kill an elevated helper; a closed pipe makes it exit
                self.commands.close()
            self.commands = None
        self.connected = False
        address = rf"\\.\pipe\que-input-{secrets.token_hex(16)}"
        authkey = secrets.token_bytes(32)
        try:
            listener = Listener(address, family="AF_PIPE", authkey=authkey)
            if self.elevate:
                # One prompt per launch: UAC takes focus from the active app, so a restart never asks
                self.elevate = False
                self.deadline = math.inf  # The UAC prompt waits for the user
                threading.Thread(
                    target=self._launch_elevated,
                    args=(listener, address, authkey, self.generation),
                    daemon=True,
                ).start()
                return
            if self.admin_notice:
                self.admin_notice = False
                self.failed.emit(ELEVATION_DECLINED)
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

    def _launch_elevated(
        self, listener: Listener, address: str, authkey: bytes, generation: int
    ) -> None:
        """Start the helper as administrator behind a UAC prompt, then connect to it.

        Args:
            listener (Listener): The authenticated pipe listener.
            address (str): The named pipe the helper connects to.
            authkey (bytes): The pipe secret, passed on the command line because an
                elevated process never inherits this process's environment.
            generation (int): The helper instance this thread serves.
        """
        try:
            process = ElevatedProcess(hook_host_command(address) + [authkey.hex()])
        except OSError:  # Declined or not allowed
            listener.close()
            self.received.emit({"type": "elevation_declined", "generation": generation})
            return
        if generation == self.generation:  # Otherwise `_connect` closes it and it exits
            self.process = process
            self.deadline = time.monotonic() + STARTUP_SECONDS
        self._connect(listener, generation)

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
        with contextlib.suppress(RuntimeError):  # Que quit and Qt deleted this object
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


class ElevatedProcess:
    """Start a process as administrator and offer the `Popen` calls Que uses."""

    def __init__(self, command: list[str]) -> None:
        info = _ShellExecuteInfo()
        info.cbSize = ctypes.sizeof(_ShellExecuteInfo)
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
        info.lpVerb = "runas"
        info.lpFile = command[0]
        info.lpParameters = subprocess.list2cmdline(command[1:])
        info.lpDirectory = os.getcwd()
        info.nShow = SW_HIDE
        if not _shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = int(info.hProcess)

    def poll(self) -> int | None:
        """Return the exit code, or None while the process runs.

        Returns:
            int | None: The exit code once the process has exited.
        """
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            return None
        return None if code.value == STILL_ACTIVE else int(code.value)

    def kill(self) -> None:
        """End the process; it also exits by itself once Que disconnects."""
        _kernel32.TerminateProcess(self.handle, 1)

    def wait(self, timeout: float) -> int:
        """Wait for the process to exit.

        Args:
            timeout (float): The longest wait in seconds.

        Returns:
            int: The exit code.
        """
        _kernel32.WaitForSingleObject(self.handle, int(timeout * 1000))
        code = self.poll()
        if code is None:
            raise subprocess.TimeoutExpired("elevated input helper", timeout)
        return code


class _ShellExecuteInfo(ctypes.Structure):
    """Mirror the Win32 SHELLEXECUTEINFOW structure."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetExitCodeProcess.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
