"""Collect private Win32 diagnostics, never served to the observer page."""

import ctypes
import json
import platform
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import win32api  # type: ignore[import-untyped]
import win32gui  # type: ignore[import-untyped]
import win32process  # type: ignore[import-untyped]
from pywintypes import error as WindowsError  # type: ignore[import-untyped]

from stealth.isolation import digest


def environment() -> dict[str, Any]:
    """Record Windows build, monitor geometry/DPI and keyboard layout."""
    displays = []
    shcore = ctypes.WinDLL("shcore")
    awareness_result = int(shcore.SetProcessDpiAwareness(2))
    shcore.GetDpiForMonitor.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
    ]
    for handle, _, _ in win32api.EnumDisplayMonitors():
        info = dict(win32api.GetMonitorInfo(handle))
        x, y = wintypes.UINT(), wintypes.UINT()
        result = shcore.GetDpiForMonitor(
            int(handle), 0, ctypes.byref(x), ctypes.byref(y)
        )
        info.update(
            dpi=[x.value, y.value] if result == 0 else None, dpi_result=int(result)
        )
        displays.append(info)
    return {
        "windows": platform.platform(),
        "version": platform.version(),
        "monitors": displays,
        "keyboard_layout": win32api.GetKeyboardLayout(),
        "dpi_awareness_result": awareness_result,
        "time_ns": time.time_ns(),
        "dpi_note": "Native coordinates; inspect actual display settings to confirm the required scaling configuration.",
    }


def audio_environment() -> dict[str, Any]:
    """Enumerate real microphone/output/loopback devices without starting recording."""
    import pyaudiowpatch  # type: ignore[import-untyped]

    with pyaudiowpatch.PyAudio() as audio:
        devices = [
            dict(audio.get_device_info_by_index(index))
            for index in range(audio.get_device_count())
        ]
        return {
            "devices": devices,
            "host_apis": [
                dict(audio.get_host_api_info_by_index(index))
                for index in range(audio.get_host_api_count())
            ],
        }


def processes_and_sockets(pid: int) -> dict[str, Any]:
    """Inventory only the target process tree and its TCP/UDP endpoints."""
    script = f"""$ErrorActionPreference = 'Stop'
$items = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name)
$owned = @({int(pid)})
do {{ $prior = $owned.Count; foreach ($item in $items) {{ if ($owned -contains $item.ParentProcessId -and $owned -notcontains $item.ProcessId) {{ $owned += $item.ProcessId }} }} }} while ($owned.Count -gt $prior)
$tcp = @(Get-NetTCPConnection | Where-Object {{ $owned -contains $_.OwningProcess }} | Select-Object LocalAddress,LocalPort,RemoteAddress,RemotePort,State,OwningProcess)
$udp = @(Get-NetUDPEndpoint | Where-Object {{ $owned -contains $_.OwningProcess }} | Select-Object LocalAddress,LocalPort,OwningProcess)
@{{processes=@($items | Where-Object {{ $owned -contains $_.ProcessId }});tcp=$tcp;udp=$udp}} | ConvertTo-Json -Depth 5 -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise OSError("Native socket/process inventory failed.")
    return dict(json.loads(result.stdout))


def window_sample(pids: set[int]) -> dict[str, Any]:
    """Sample independent z-order/rectangles and affinity for owned visible windows."""
    windows: list[dict[str, Any]] = []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowDisplayAffinity.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowDisplayAffinity.restype = wintypes.BOOL

    def visit(hwnd: int, _: Any) -> bool:
        """Retain geometry without collecting unrelated window titles."""
        if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            _, owner = win32process.GetWindowThreadProcessId(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            owned = owner in pids
            affinity = wintypes.DWORD()
            ok = (
                bool(user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)))
                if owned
                else False
            )
            windows.append(
                {
                    "hwnd": hwnd,
                    "owned": owned,
                    "rect": rect,
                    "topmost_style": bool(win32gui.GetWindowLong(hwnd, -20) & 8),
                    "pid": owner if owned else None,
                    "affinity": affinity.value if ok else None,
                    "affinity_error": ctypes.get_last_error()
                    if owned and not ok
                    else None,
                }
            )
        return True

    win32gui.EnumWindows(visit, None)
    owned_windows = []
    for index, window in enumerate(windows):
        if not window["owned"]:
            continue
        left, top, right, bottom = window["rect"]
        overlaps = [
            other["hwnd"]
            for other in windows[:index]
            if not other["owned"]
            and min(right, other["rect"][2]) > max(left, other["rect"][0])
            and min(bottom, other["rect"][3]) > max(top, other["rect"][1])
        ]
        owned_windows.append(
            {**window, "z_index": index, "potential_occluders": overlaps}
        )
    return {
        "wall_ms": time.time_ns() // 1_000_000,
        "monotonic_ns": time.perf_counter_ns(),
        "windows": owned_windows,
        "clipboard_sequence": int(user32.GetClipboardSequenceNumber()),
    }


class NativeRecorder:
    """Record sampled windows and WinEvent changes on a daemon thread."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.pid = 0
        self.pids: set[int] = set()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.socket_thread = threading.Thread(target=self._sockets, daemon=True)

    def start(self) -> None:
        """Start independent desktop and socket observers."""
        self.thread.start()
        self.socket_thread.start()

    def stop(self) -> None:
        """Flush collectors and finish before writing final reports."""
        self.stop_event.set()
        self.thread.join(timeout=5)
        self.socket_thread.join(timeout=35)

    def _sockets(self) -> None:
        """Update descendant ownership and connections without delaying window sampling."""
        with (self.directory / "sockets.jsonl").open("a", encoding="utf-8") as handle:
            while not self.stop_event.is_set():
                if self.pid:
                    try:
                        data = processes_and_sockets(self.pid)
                        self.pids = {
                            int(item["ProcessId"]) for item in data["processes"]
                        } | {self.pid}
                        handle.write(
                            json.dumps({"wall_ms": time.time_ns() // 1_000_000, **data})
                            + "\n"
                        )
                    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                        handle.write(
                            json.dumps(
                                {
                                    "error": type(exc).__name__,
                                    "wall_ms": time.time_ns() // 1_000_000,
                                }
                            )
                            + "\n"
                        )
                    handle.flush()
                self.stop_event.wait(5)

    def _run(self) -> None:
        """Observe z-order events and sample every 50 ms, reporting actual intervals."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        user32.SetWinEventHook.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HMODULE,
            callback_type,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        with (self.directory / "windows.jsonl").open("a", encoding="utf-8") as handle:

            def on_event(
                hook: Any,
                event: int,
                hwnd: int,
                obj: int,
                child: int,
                thread: int,
                timestamp: int,
            ) -> None:
                """Record owned window creation, destruction, movement and reorder events."""
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid in self.pids | {self.pid}:
                        handle.write(
                            json.dumps(
                                {
                                    "type": "winevent",
                                    "event": event,
                                    "hwnd": hwnd,
                                    "wall_ms": time.time_ns() // 1_000_000,
                                    "windows_tick": timestamp,
                                }
                            )
                            + "\n"
                        )
                except WindowsError:
                    handle.write(
                        json.dumps({"type": "destroyed-window-race", "hwnd": hwnd})
                        + "\n"
                    )

            callback = callback_type(on_event)
            hook = user32.SetWinEventHook(0x8000, 0x800B, None, callback, 0, 0, 0)
            handle.write(
                json.dumps({"winevent_installed": bool(hook), "sample_target_ms": 50})
                + "\n"
            )
            previous = time.perf_counter_ns()
            try:
                while not self.stop_event.is_set():
                    win32gui.PumpWaitingMessages()
                    try:
                        sample = window_sample(self.pids | {self.pid})
                        now = time.perf_counter_ns()
                        sample["interval_ms"] = (now - previous) / 1_000_000
                        previous = now
                        handle.write(json.dumps(sample) + "\n")
                        handle.flush()
                    except (OSError, WindowsError) as exc:
                        handle.write(json.dumps({"error": type(exc).__name__}) + "\n")
                    self.stop_event.wait(0.05)
            finally:
                if hook:
                    user32.UnhookWinEvent(hook)


def runtime_files(directory: Path) -> list[dict[str, Any]]:
    """Hash synthetic screenshots and model files without exporting credentials or chats."""
    roots = [directory / "isolated" / "data" / "cache", directory / "private" / "temp"]
    return [
        {
            "path": str(path.relative_to(directory)),
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.resolve().is_relative_to(directory.resolve())
        and not path.is_symlink()
    ]
