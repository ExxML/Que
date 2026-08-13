import ctypes
from ctypes import wintypes

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QWidget


# Windows display-affinity constant that excludes a window from screen capture (Windows 10 2004+)
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def exclude_from_capture(widget: QWidget) -> bool:
    """Exclude the window from screen capture via SetWindowDisplayAffinity.

    Args:
        widget (QWidget): The top-level widget whose native window is excluded.

    Returns:
        bool: True if the affinity was applied successfully.
    """
    hwnd = int(widget.winId())
    result = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    return int(result) != 0


def set_always_on_top(_widget: QWidget) -> None:
    """Apply any native always-on-top reinforcement.

    On Windows the Qt WindowStaysOnTopHint flag is sufficient; the periodic
    re-raise handled by the caller covers the cases where Windows overrides it,
    so no additional native call is required here.
    """
    return None


def is_topmost_at_corners(widget: QWidget, device_pixel_ratio: float) -> bool:
    """Check whether the widget is the topmost window at its sampled corner points.

    Args:
        widget (QWidget): The top-level widget to test.
        device_pixel_ratio (float): The screen's device pixel ratio for scaling points.

    Returns:
        bool: True if the widget is topmost at all sampled corners.
    """
    try:
        rect = widget.frameGeometry()
        ga_root = 2
        self_hwnd = int(widget.winId())
        self_root = ctypes.windll.user32.GetAncestor(wintypes.HWND(self_hwnd), ga_root)
        padding = 15
        corners = [
            rect.topLeft() + QPoint(padding, padding),
            rect.topRight() + QPoint(-padding, padding),
            rect.bottomLeft() + QPoint(padding, -padding),
            rect.bottomRight() + QPoint(-padding, -padding),
        ]
        for corner in corners:
            pt = wintypes.POINT(
                int(corner.x() * device_pixel_ratio),
                int(corner.y() * device_pixel_ratio),
            )
            hwnd_at_pt = ctypes.windll.user32.WindowFromPoint(pt)
            if hwnd_at_pt:
                target_root = ctypes.windll.user32.GetAncestor(
                    wintypes.HWND(hwnd_at_pt), ga_root
                )
                if int(self_root) != int(target_root):
                    return False
        return True
    except Exception:
        return True
