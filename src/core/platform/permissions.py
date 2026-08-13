import sys

if sys.platform == "darwin":
    from core.platform.macos import permissions as _backend
else:
    from core.platform.windows import permissions as _backend


def ensure_accessibility(prompt: bool = True) -> bool:
    """Ensure accessibility permission via the platform backend.

    Args:
        prompt (bool): Whether to show the system permission dialog if not granted.

    Returns:
        bool: True if accessibility permission is granted.
    """
    return _backend.ensure_accessibility(prompt)


def ensure_screen_recording(prompt: bool = True) -> bool:
    """Ensure screen-recording permission via the platform backend.

    Args:
        prompt (bool): Whether to request access (showing the system dialog) if not granted.

    Returns:
        bool: True if screen-recording permission is granted.
    """
    return _backend.ensure_screen_recording(prompt)
