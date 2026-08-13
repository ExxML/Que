import sys

from PyQt6.QtWidgets import QWidget

if sys.platform == "darwin":
    from core.platform.macos import window_effects as _backend
else:
    from core.platform.windows import window_effects as _backend


def exclude_from_capture(widget: QWidget) -> bool:
    """Exclude a window from screen capture using the platform backend.

    Args:
        widget (QWidget): The top-level widget whose native window is excluded.

    Returns:
        bool: True if the exclusion was applied successfully.
    """
    return _backend.exclude_from_capture(widget)


def set_always_on_top(widget: QWidget) -> None:
    """Apply native always-on-top reinforcement using the platform backend.

    Args:
        widget (QWidget): The top-level widget whose native window is configured.
    """
    _backend.set_always_on_top(widget)


def is_topmost_at_corners(widget: QWidget, device_pixel_ratio: float) -> bool:
    """Check whether a widget is topmost using the platform backend.

    Args:
        widget (QWidget): The top-level widget to test.
        device_pixel_ratio (float): The screen's device pixel ratio for scaling points.

    Returns:
        bool: True if the widget is topmost (always True on macOS).
    """
    return _backend.is_topmost_at_corners(widget, device_pixel_ratio)
