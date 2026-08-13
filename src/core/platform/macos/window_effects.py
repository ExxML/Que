import objc
from AppKit import (
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
)
from PyQt6.QtWidgets import QWidget


# Window level that floats above nearly all normal windows, including full-screen apps
_NS_SCREEN_SAVER_WINDOW_LEVEL = 1000


def exclude_from_capture(widget: QWidget) -> bool:
    """Exclude the window from screen capture via NSWindow sharingType.

    Setting sharingType to NSWindowSharingNone hides the window from the
    application's own screenshots, legacy recorders, and CGWindowList captures.
    It does not guarantee exclusion from every ScreenCaptureKit-based recorder.

    Args:
        widget (QWidget): The top-level widget whose native window is excluded.

    Returns:
        bool: True if the native window was found and updated.
    """
    ns_window = _native_window(widget)
    if ns_window is None:
        return False
    ns_window.setSharingType_(NSWindowSharingNone)
    return True


def set_always_on_top(widget: QWidget) -> None:
    """Raise the window level and let it appear on all spaces and full-screen apps.

    Args:
        widget (QWidget): The top-level widget whose native window is configured.
    """
    ns_window = _native_window(widget)
    if ns_window is None:
        return None
    ns_window.setLevel_(_NS_SCREEN_SAVER_WINDOW_LEVEL)
    ns_window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    return None


def is_topmost_at_corners(_widget: QWidget, _device_pixel_ratio: float) -> bool:
    """Report the window as topmost so the caller relies on the periodic re-raise.

    macOS has no cheap, non-private per-point window hit-test equivalent to the
    Windows WindowFromPoint check. The screen-saver window level plus the
    caller's periodic raise keeps the overlay on top, so this always reports
    True and defers to that mechanism.

    Returns:
        bool: Always True on macOS.
    """
    return True


def _native_window(widget: QWidget) -> NSWindow | None:
    """Return the NSWindow backing a Qt widget, or None if unavailable.

    Args:
        widget (QWidget): The top-level widget to resolve.

    Returns:
        NSWindow: The backing NSWindow, or None if it cannot be resolved.
    """
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
    except Exception:
        return None
    ns_window: NSWindow | None = ns_view.window()
    return ns_window
