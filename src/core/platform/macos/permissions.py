import Quartz
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    kAXTrustedCheckOptionPrompt,
)


def ensure_accessibility(prompt: bool = True) -> bool:
    """Return whether the process is trusted for Accessibility, optionally prompting.

    An Accessibility grant is required for a CGEventTap to observe and suppress
    keystrokes system-wide. When prompt is True and the process is not yet
    trusted, macOS shows the system dialog directing the user to
    System Settings > Privacy & Security > Accessibility.

    Args:
        prompt (bool): Whether to show the system permission dialog if untrusted.

    Returns:
        bool: True if the process is currently trusted for Accessibility.
    """
    if not prompt:
        return bool(AXIsProcessTrusted())
    options = {kAXTrustedCheckOptionPrompt: True}
    return bool(AXIsProcessTrustedWithOptions(options))


def ensure_screen_recording(prompt: bool = True) -> bool:
    """Return whether screen-recording access is granted, optionally requesting it.

    Screen-recording access is required to capture the screen on macOS 10.15+.
    When prompt is True and access has not been granted, macOS shows the system
    dialog directing the user to
    System Settings > Privacy & Security > Screen Recording.

    Args:
        prompt (bool): Whether to request access (showing the system dialog) if not granted.

    Returns:
        bool: True if screen-recording access is currently granted.
    """
    if bool(Quartz.CGPreflightScreenCaptureAccess()):
        return True
    if prompt:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    return False
