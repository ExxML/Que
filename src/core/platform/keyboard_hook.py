import sys

from core.platform.keyboard_hook_base import (
    HotkeyEntry,
    HotkeyHost,
    HotkeyTable,
    Key,
    KeyboardHook,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
)

__all__ = [
    "HotkeyEntry",
    "HotkeyHost",
    "HotkeyTable",
    "Key",
    "KeyboardHook",
    "MOD_ALT",
    "MOD_CTRL",
    "MOD_SHIFT",
    "create_keyboard_hook",
]


def create_keyboard_hook(host: HotkeyHost) -> KeyboardHook:
    """Create the platform-appropriate keyboard-hook backend.

    Args:
        host (HotkeyHost): The shortcut manager that owns the hotkey tables and callbacks.

    Returns:
        KeyboardHook: The Windows or macOS keyboard-hook backend.
    """
    if sys.platform == "darwin":
        from core.platform.macos.keyboard_hook import MacKeyboardHook

        return MacKeyboardHook(host)

    from core.platform.windows.keyboard_hook import WindowsKeyboardHook

    return WindowsKeyboardHook(host)
