from enum import Enum
from typing import Callable, Protocol


# Modifier bitmask values used in hotkey lookup tables (platform-neutral; Que's own constants)
MOD_CTRL = 0x01
MOD_ALT = 0x02
MOD_SHIFT = 0x04


class Key(Enum):
    """Platform-neutral token for a physical key used in a hotkey combination.

    Hotkey tables are keyed by these tokens so a single table resolves against
    Windows virtual-key codes and macOS keycodes alike. Each platform backend
    owns a small map from its native keycodes to these tokens.
    """

    D = "D"
    E = "E"
    G = "G"
    N = "N"
    Q = "Q"
    S = "S"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    UP = "UP"
    DOWN = "DOWN"


# A hotkey table maps (modifier_bitmask, key) to (callback, repeat_allowed)
HotkeyEntry = tuple[Callable[[], None], bool]
HotkeyTable = dict[tuple[int, Key], HotkeyEntry]


class HotkeyHost(Protocol):
    """Narrow view of the shortcut manager that a keyboard-hook backend consumes."""

    def is_main_window_visible(self) -> bool:
        """Return whether the main window is currently visible."""
        ...

    def resolve_hotkey(self, modifiers: int, key: Key) -> HotkeyEntry | None:
        """Return the hotkey entry matching the modifier mask and key, or None.

        Args:
            modifiers (int): Bitmask of currently held modifier keys.
            key (Key): The non-modifier key token pressed.

        Returns:
            HotkeyEntry: The (callback, repeat_allowed) tuple, or None if no match.
        """
        ...


class KeyboardHook(Protocol):
    """Platform keyboard-hook backend that installs and removes the global hook."""

    def start(self) -> None:
        """Install the global keyboard hook and begin delivering events."""
        ...

    def stop(self) -> None:
        """Remove the global keyboard hook and stop delivering events."""
        ...
