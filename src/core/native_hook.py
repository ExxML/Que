"""Drive the compiled hook engine (assets/que_hook.dll), whose hook callbacks never run Python.

The library installs the low-level hooks on its own native thread and decides every
key and click with the same rules as `HookEngine`, so Python's interpreter lock,
allocator and paging can never delay a callback past Windows' deadline. This wrapper
configures it from the policy tables in `core.hook_engine`, forwards Que's state to it
and turns its decisions back into the events `HookEngine` would emit.
"""

import ctypes
import ctypes.wintypes
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.hook_engine import (
    ACTION_NAMES,
    CHORD_BITS,
    DEVICE_KEYS,
    HOTKEYS,
    LOCK_KEYS,
    QUE_KEY,
    SYSTEM_CHORDS,
    WINDOWS_KEYS,
    InputMonitor,
    key_identity,
)

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "assets" / "que_hook.dll"
MAX_REGIONS = 32
STATUS_KEYBOARD = 0x01
STATUS_MOUSE = 0x02
STATUS_MEMORY_LOCKED = 0x04
STATUS_PRIORITY = 0x08
CLASS_DEVICE = 0x01
CLASS_LOCK = 0x02
CLASS_WINDOWS = 0x04
COMMANDS = {
    "visible": 1,
    "armed": 2,
    "routing": 3,
    "regions": 4,
    "quit": 5,
    "release_all": 6,
}
EVENT_ACTION = 1
EVENT_LOCK = 2
EVENT_ARMED = 3
EVENT_KEY = 4
EVENT_POINTER = 5
EVENT_QUIT_READY = 6
EVENT_OBSERVE_KEY = 7
EVENT_OBSERVE_BUTTON = 8
EVENT_OBSERVE_WHEEL = 9
COMMAND_RETRY_S = 2.0


class NativeHookEngine:
    """Own consumed keys and clicks through the compiled engine, with `HookEngine`'s interface.

    Before `start`, decisions run synchronously on the caller's thread through
    `process_key` and `process_pointer`, which lets tests compare the compiled
    engine with `HookEngine` event by event.
    """

    def __init__(
        self, emit: Callable[[dict[str, Any]], None], path: Path = LIBRARY_PATH
    ) -> None:
        self.emit = emit
        self.library = ctypes.CDLL(str(path))
        _declare(self.library)
        self.handle = self.library.que_create()
        if not self.handle:
            raise OSError("Could not allocate the native hook engine.")
        self.started = False
        self.dropped = 0
        self.modifiers = sorted(CHORD_BITS)
        self._monitor: InputMonitor | None = None
        self._configure()

    @property
    def monitor(self) -> InputMonitor | None:
        """Return the Raw Input comparison that receives what the hooks observe.

        Returns:
            InputMonitor | None: The monitor, or None when leak detection is unavailable.
        """
        return self._monitor

    @monitor.setter
    def monitor(self, monitor: InputMonitor | None) -> None:
        """Attach or detach the Raw Input comparison.

        Args:
            monitor (InputMonitor | None): The monitor, or None to stop reporting observations.
        """
        self._monitor = monitor
        self.library.que_set_observe(self.handle, monitor is not None)

    def apply(self, message: dict[str, Any]) -> None:
        """Apply one state update from Que.

        Args:
            message (dict[str, Any]): The update; its "type" names the state that changed.
        """
        kind = message.get("type")
        command = _Command()
        command.type = COMMANDS.get(str(kind), 0)
        if not command.type:
            return
        if kind in ("visible", "armed", "routing"):
            command.value = bool(message["value"])
        if kind == "armed":
            command.seq = int(message["seq"])
        if kind == "regions":
            regions = list(message["value"])[:MAX_REGIONS]
            command.count = len(regions)
            for index, (hwnd, left, top, right, bottom) in enumerate(regions):
                command.regions[index] = _Region(
                    int(hwnd), int(left), int(top), int(right), int(bottom)
                )
        self._send(command)

    def release_all_state(self) -> None:
        """Stop claiming new input when Que disconnects, while still owning held presses."""
        command = _Command()
        command.type = COMMANDS["release_all"]
        self._send(command)

    def drained(self) -> bool:
        """Report whether no consumed key, buffered modifier or owned click is still held.

        Returns:
            bool: Whether every press Que owns has been physically released.
        """
        return bool(self.library.que_drained(self.handle))

    def request_quit(self) -> None:
        """Defer application shutdown until all swallowed physical input is released."""
        self.apply({"type": "quit"})

    def start(self, window: int, message: int) -> int:
        """Start the hook thread and install both hooks.

        Args:
            window (int): The window that receives `message` when decisions are waiting.
            message (int): The window message to post.

        Returns:
            int: STATUS_* flags; without STATUS_KEYBOARD the hooks are not installed.
        """
        status = int(self.library.que_start(self.handle, window, message))
        self.started = bool(status & STATUS_KEYBOARD)
        return status

    def reinstall(self) -> int:
        """Remove and reinstall both hooks, putting them first in Windows' hook chain.

        Returns:
            int: STATUS_* flags; without STATUS_KEYBOARD the hooks are not installed.
        """
        return int(self.library.que_reinstall(self.handle))

    def stop(self) -> None:
        """Remove the hooks and end the hook thread."""
        self.library.que_stop(self.handle)
        self.started = False

    def stats(self) -> dict[str, int]:
        """Return the hook thread's status and timing counters.

        Returns:
            dict[str, int]: Status flags, install errors, late events and dropped events.
        """
        stats = _Stats()
        self.library.que_stats(self.handle, ctypes.byref(stats))
        return {field[0]: int(getattr(stats, field[0])) for field in _Stats._fields_}

    def drain(self) -> None:
        """Emit every decision the hook thread has made since the last drain."""
        self.library.que_ack_notify(self.handle)
        event = _Event()
        while self.library.que_next_event(self.handle, ctypes.byref(event)):
            self._dispatch(event)
        dropped = self.stats()["dropped_events"]
        if dropped > self.dropped:
            self.emit(
                {
                    "type": "error",
                    "text": f"Que's input helper lost {dropped - self.dropped} input "
                    "events (its queue was full).",
                }
            )
            self.dropped = dropped

    def process_key(
        self, code: int, message: int, vk: int, scan: int, flags: int
    ) -> int:
        """Decide one keyboard event on this thread (only before `start`).

        Args:
            code (int): The native hook code.
            message (int): The keyboard message.
            vk (int): The virtual key code.
            scan (int): The hardware scan code.
            flags (int): The low-level hook flags.

        Returns:
            int: One for consumed input, otherwise zero.
        """
        result = int(
            self.library.que_test_key(self.handle, code, message, vk, scan, flags)
        )
        self.drain()
        return result

    def process_pointer(
        self, code: int, message: int, x: int, y: int, data: int, flags: int
    ) -> int:
        """Decide one mouse event on this thread (only before `start`).

        Args:
            code (int): The native hook code.
            message (int): The mouse message.
            x (int): The physical desktop x coordinate.
            y (int): The physical desktop y coordinate.
            data (int): The wheel delta or extra-button identifier.
            flags (int): The low-level hook flags.

        Returns:
            int: One for consumed input, otherwise zero.
        """
        result = int(
            self.library.que_test_pointer(self.handle, code, message, x, y, data, flags)
        )
        self.drain()
        return result

    def replace_sender(
        self, sender: Callable[[list[tuple[int, int, int]]], None]
    ) -> None:
        """Send replayed keys to `sender` instead of SendInput (tests, only before `start`).

        Windows re-enters the hook callback for each injected key before SendInput
        returns, so a test sender should feed the replays back in as injected events.

        Args:
            sender (Callable[[list[tuple[int, int, int]]], None]): Receives
                (virtual key, scan code, keybd_event flags) per replayed key, in order.
        """

        def send(replays: Any, count: int) -> None:
            """Convert the native replay array and pass it to the sender."""
            sender(
                [
                    (replays[i].vk, replays[i].scan, replays[i].flags)
                    for i in range(count)
                ]
            )

        self.sender = _SENDER(
            send
        )  # Referenced here so ctypes keeps the callback alive
        self.library.que_test_set_sender(self.handle, self.sender)

    def state(self) -> dict[str, Any]:
        """Return the engine's held and owned input for comparison in tests.

        Returns:
            dict[str, Any]: Held keys, owned keys, held modifiers and counters.
        """
        held, suppressed, modifiers = (
            (ctypes.c_uint8 * 256)(),
            (ctypes.c_uint8 * 256)(),
            (ctypes.c_uint8 * 256)(),
        )
        counts = (ctypes.c_int32 * 4)()
        self.library.que_test_state(self.handle, held, suppressed, modifiers, counts)
        return {
            "held_vk_codes": {vk for vk in range(256) if held[vk]},
            "suppressed_vk_codes": {vk for vk in range(256) if suppressed[vk]},
            "held_modifier_vks": {vk for vk in range(256) if modifiers[vk]},
            "buffered_modifiers": counts[0],
            "owned_buttons": counts[1],
            "input_armed": bool(counts[2]),
            "press_seq": counts[3],
        }

    def close(self) -> None:
        """Stop the hooks if running and free the engine."""
        if self.started:
            self.stop()
        if self.handle:
            self.library.que_destroy(self.handle)
            self.handle = None

    def _configure(self) -> None:
        """Load the hotkey, modifier and key-class tables into the compiled engine."""
        for mask, vk, name, repeat, always in HOTKEYS:
            self.library.que_add_hotkey(
                self.handle, mask, vk, ACTION_NAMES.index(name), repeat, always
            )
        for vk in self.modifiers:
            self.library.que_add_modifier(self.handle, vk, CHORD_BITS[vk])
        self.library.que_set_que_key(self.handle, QUE_KEY)
        for vk in DEVICE_KEYS | LOCK_KEYS | WINDOWS_KEYS:
            self.library.que_set_key_class(
                self.handle,
                vk,
                (CLASS_DEVICE if vk in DEVICE_KEYS else 0)
                | (CLASS_LOCK if vk in LOCK_KEYS else 0)
                | (CLASS_WINDOWS if vk in WINDOWS_KEYS else 0),
            )
        for mask, vk in SYSTEM_CHORDS:
            self.library.que_add_system_chord(self.handle, mask, vk)

    def _send(self, command: "_Command") -> None:
        """Queue a state update for the hook thread, applying it at once before `start`.

        Args:
            command (_Command): The state update.
        """
        deadline = time.monotonic() + COMMAND_RETRY_S
        while not self.library.que_command(self.handle, ctypes.byref(command)):
            if time.monotonic() > deadline:
                self.emit(
                    {
                        "type": "error",
                        "text": "Que's input helper stopped taking updates.",
                    }
                )
                return
            time.sleep(0.001)
        if not self.started:
            self.drain()

    def _dispatch(self, event: "_Event") -> None:
        """Turn one compiled-engine event into the event `HookEngine` would emit.

        Args:
            event (_Event): The event from the hook thread.
        """
        held = [
            vk for index, vk in enumerate(self.modifiers) if event.held >> index & 1
        ]
        if event.type == EVENT_ACTION:
            self.emit({"type": "action", "name": ACTION_NAMES[event.a]})
        elif event.type == EVENT_LOCK:
            self.emit({"type": "lock", "vk": event.a})
        elif event.type == EVENT_ARMED:
            self.emit({"type": "armed", "value": bool(event.a), "seq": event.seq})
        elif event.type == EVENT_KEY:
            self.emit(
                {
                    "type": "key",
                    "vk": event.a,
                    "scan": event.b,
                    "flags": event.c,
                    "down": bool(event.d & 1),
                    "repeated": bool(event.d & 2),
                    "held": held,
                }
            )
        elif event.type == EVENT_POINTER:
            self.emit(
                {
                    "type": "pointer",
                    "message": event.a,
                    "x": event.b,
                    "y": event.c,
                    "data": event.d,
                    "hwnd": event.hwnd,
                    "seq": event.seq,
                    "held": held,
                }
            )
        elif event.type == EVENT_QUIT_READY:
            self.emit({"type": "quit_ready"})
        elif self._monitor is not None:
            if event.type == EVENT_OBSERVE_KEY:
                identity = key_identity(event.a, event.b, bool(event.c), bool(event.d))
            elif event.type == EVENT_OBSERVE_BUTTON:
                identity = frozenset({("button", event.a, bool(event.d))})
            else:
                identity = frozenset({("wheel", event.a)})
            self._monitor.record_hook(identity, event.tick)


def _declare(library: ctypes.CDLL) -> None:
    """Declare the compiled engine's function signatures.

    Args:
        library (ctypes.CDLL): The loaded hook library.
    """
    handle = ctypes.c_void_p
    signatures: dict[str, tuple[Any, list[Any]]] = {
        "que_create": (handle, []),
        "que_destroy": (None, [handle]),
        "que_add_hotkey": (ctypes.c_int, [handle, *[ctypes.c_int] * 5]),
        "que_add_modifier": (ctypes.c_int, [handle, ctypes.c_int, ctypes.c_int]),
        "que_set_key_class": (None, [handle, ctypes.c_int, ctypes.c_int]),
        "que_add_system_chord": (ctypes.c_int, [handle, ctypes.c_int, ctypes.c_int]),
        "que_set_observe": (None, [handle, ctypes.c_int]),
        "que_set_que_key": (None, [handle, ctypes.c_int]),
        "que_command": (ctypes.c_int, [handle, ctypes.POINTER(_Command)]),
        "que_start": (
            ctypes.c_int,
            [handle, ctypes.wintypes.HWND, ctypes.wintypes.UINT],
        ),
        "que_reinstall": (ctypes.c_int, [handle]),
        "que_stop": (None, [handle]),
        "que_ack_notify": (None, [handle]),
        "que_next_event": (ctypes.c_int, [handle, ctypes.POINTER(_Event)]),
        "que_drained": (ctypes.c_int, [handle]),
        "que_stats": (None, [handle, ctypes.POINTER(_Stats)]),
        "que_test_key": (ctypes.c_int, [handle, *[ctypes.c_int] * 5]),
        "que_test_pointer": (
            ctypes.c_int,
            [handle, *[ctypes.c_int] * 4, ctypes.c_uint32, ctypes.c_int],
        ),
        "que_test_set_sender": (None, [handle, _SENDER]),
        "que_test_state": (
            None,
            [
                handle,
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.POINTER(ctypes.c_int32),
            ],
        ),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes


class _Region(ctypes.Structure):
    """Represent QueRegion: a Que window and its physical desktop rectangle."""

    _fields_ = [
        ("hwnd", ctypes.c_int64),
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("right", ctypes.c_int32),
        ("bottom", ctypes.c_int32),
    ]


class _Command(ctypes.Structure):
    """Represent QueCommand: one state update for the hook thread."""

    _fields_ = [
        ("type", ctypes.c_int32),
        ("value", ctypes.c_int32),
        ("seq", ctypes.c_uint32),
        ("count", ctypes.c_int32),
        ("regions", _Region * MAX_REGIONS),
    ]


class _Event(ctypes.Structure):
    """Represent QueEvent: one decision or observation from the hook thread."""

    _fields_ = [
        ("type", ctypes.c_int32),
        ("a", ctypes.c_int32),
        ("b", ctypes.c_int32),
        ("c", ctypes.c_int32),
        ("d", ctypes.c_uint32),
        ("seq", ctypes.c_uint32),
        ("held", ctypes.c_uint32),
        ("tick", ctypes.c_uint32),
        ("hwnd", ctypes.c_int64),
    ]


class _Replay(ctypes.Structure):
    """Represent QueReplay: one replayed key event."""

    _fields_ = [
        ("vk", ctypes.c_int32),
        ("scan", ctypes.c_int32),
        ("flags", ctypes.c_int32),
    ]


class _Stats(ctypes.Structure):
    """Represent QueStats: the hook thread's status and timing counters."""

    _fields_ = [
        ("status", ctypes.c_uint32),
        ("keyboard_error", ctypes.c_uint32),
        ("mouse_error", ctypes.c_uint32),
        ("late_events", ctypes.c_uint32),
        ("max_late_ms", ctypes.c_uint32),
        ("dropped_events", ctypes.c_uint32),
    ]


_SENDER = ctypes.CFUNCTYPE(None, ctypes.POINTER(_Replay), ctypes.c_int)
