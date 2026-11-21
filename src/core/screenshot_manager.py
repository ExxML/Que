import ctypes
from ctypes import wintypes
from pathlib import Path

import mss
from PyQt6.QtCore import QObject, pyqtSignal

from core.window_policy import native

MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    """Win32 MONITORINFO: a monitor's full and work-area rectangles."""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


native.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
native.MonitorFromWindow.restype = wintypes.HMONITOR
native.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
native.GetMonitorInfoW.restype = wintypes.BOOL


class ScreenshotManager(QObject):
    """Capture screenshots of the screen Que is on."""

    screenshot_added = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.screenshots_dir = (
            Path(__file__).resolve().parent.parent / "data" / "cache" / "screenshots"
        )
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_count = 0
        self.pending_paths: list[str] = []

    def take_screenshot(self, hwnd: int) -> str:
        """Take a screenshot of the monitor showing a window and save it to the screenshots directory.

        Args:
            hwnd (int): The window whose monitor (or nearest monitor) is captured; 0 captures the primary monitor.

        Returns:
            str: The filepath of the saved screenshot, or an empty string on failure.
        """
        try:
            filename = f"screenshot{self.screenshot_count}.png"
            filepath = str(self.screenshots_dir / filename)

            info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            monitor_handle = native.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if not native.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            rect = info.rcMonitor

            # Create a new mss instance for each call (thread-safe)
            with mss.mss() as sct:
                screenshot = sct.grab(
                    {
                        "left": rect.left,
                        "top": rect.top,
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top,
                    }
                )
                mss.tools.to_png(screenshot.rgb, screenshot.size, output=str(filepath))

            self.screenshot_count += 1
            self.pending_paths.append(filepath)
            self.screenshot_added.emit(filepath)
            return filepath

        except Exception as e:  # noqa: BLE001
            print(f"Error taking screenshot: {e!s}")
            return ""

    def get_and_clear_pending(self) -> list[str]:
        """Return all pending screenshot paths and clear the pending list.

        Returns:
            list[str]: The list of pending screenshot file paths.
        """
        paths = self.pending_paths.copy()
        self.pending_paths.clear()
        return paths

    def remove_pending(self, path: str) -> None:
        """Remove a specific screenshot from the pending list.

        Args:
            path (str): The file path of the screenshot to remove.
        """
        if path in self.pending_paths:
            self.pending_paths.remove(path)

    def clear_screenshots(self) -> None:
        """Delete all screenshots from the screenshots directory and reset the counter."""
        self.pending_paths.clear()

        try:
            for filepath in self.screenshots_dir.glob("screenshot*.png"):
                filepath.unlink()
        except Exception as e:  # noqa: BLE001
            print(f"Error clearing screenshots: {e!s}")

        self.screenshot_count = 0
