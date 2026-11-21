"""Decide which physical input Que consumes, without Qt and independent of the hosting process."""

import ctypes
import ctypes.wintypes
from collections import deque
from collections.abc import Callable
from typing import Any

from core.win32_hook import (
    KBDLLHOOKSTRUCT,
    KEYEVENTF_EXTENDEDKEY,
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    VK_TO_MOD_BIT,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    kernel32,
    user32,
)

# Caps Lock is Que's own shortcut key and never toggles while Que runs. It, any modifier
# pressed while it is held and any key pressed with it are withheld from other apps, so a
# page never sees anything of a Que shortcut; every other modifier reaches the active app
# untouched, with its natural timing and auto-repeat.
QUE_KEY = 0x14  # VK_CAPITAL
MOD_QUE = 0x08
CHORD_BITS: dict[int, int] = {**VK_TO_MOD_BIT, QUE_KEY: MOD_QUE}
PRESET_COUNT = 10  # One per number key, 1 to 9 then 0
VK_ESCAPE = 0x1B

# (modifier mask, virtual key, action name, repeats while held, active while hidden)
HOTKEYS: tuple[tuple[int, int, str, bool, bool], ...] = (
    (MOD_QUE, ord("E"), "toggle_window_visibility", False, True),
    (MOD_QUE, ord("R"), "toggle_recording", False, True),
    (MOD_QUE | MOD_ALT | MOD_SHIFT, ord("Q"), "close_app", False, True),
    (MOD_QUE | MOD_ALT, ord("A"), "move_window_left", True, False),
    (MOD_QUE | MOD_ALT, ord("D"), "move_window_right", True, False),
    (MOD_QUE | MOD_ALT, ord("W"), "move_window_up", True, False),
    (MOD_QUE | MOD_ALT, ord("S"), "move_window_down", True, False),
    (MOD_QUE | MOD_SHIFT, ord("A"), "resize_window_narrower", True, False),
    (MOD_QUE | MOD_SHIFT, ord("D"), "resize_window_wider", True, False),
    (MOD_QUE | MOD_SHIFT, ord("W"), "resize_window_taller", True, False),
    (MOD_QUE | MOD_SHIFT, ord("S"), "resize_window_shorter", True, False),
    (MOD_QUE, ord("W"), "scroll_up", True, False),
    (MOD_QUE, ord("S"), "scroll_down", True, False),
    (MOD_QUE, ord("G"), "screenshot", False, False),
    (MOD_QUE | MOD_SHIFT, ord("X"), "toggle_click_through", False, False),
    (MOD_QUE, ord("N"), "new_chat", False, False),
    (MOD_QUE, ord("H"), "toggle_history", False, False),
    (MOD_QUE, ord("A"), "answer", False, False),
    (MOD_QUE, ord("C"), "code", False, False),
    (MOD_QUE, ord("D"), "system_design", False, False),
    (MOD_QUE, ord("F"), "follow_up", False, False),
    (MOD_QUE, VK_ESCAPE, "stop_generation", False, False),
    # Caps Lock+1 to Caps Lock+0 apply model presets 1 to 10
    *(
        (MOD_QUE, ord(str(slot % 10)), f"preset_{slot}", False, False)
        for slot in range(1, PRESET_COUNT + 1)
    ),
)
ACTION_NAMES = tuple(name for _, _, name, _, _ in HOTKEYS)

VK_TAB = 0x09
VK_F4 = 0x73
WINDOWS_KEYS = frozenset({0x5B, 0x5C})
# Chords Windows uses to switch, close or open system UI; they leave Que's typing mode
SYSTEM_CHORDS = frozenset(
    {
        (MOD_ALT, VK_TAB),
        (MOD_ALT | MOD_SHIFT, VK_TAB),
        (MOD_ALT, VK_ESCAPE),
        (MOD_ALT | MOD_SHIFT, VK_ESCAPE),
        (MOD_CTRL, VK_ESCAPE),
        (MOD_CTRL | MOD_SHIFT, VK_ESCAPE),
        (MOD_ALT, VK_F4),
    }
)
# Function, Print Screen, sleep, Num and Scroll Lock, browser, volume, media and launch keys keep
# working while typing in Que
DEVICE_KEYS = frozenset(
    {*range(0x70, 0x88), 0x2C, 0x5F, 0x90, 0x91, *range(0xA6, 0xB8)}
)

WM_MOUSEMOVE = 0x200
WM_MOUSEWHEEL = 0x20A
WM_MOUSEHWHEEL = 0x20E
BUTTON_MESSAGES = {
    0x201: (1, True),
    0x202: (1, False),
    0x204: (2, True),
    0x205: (2, False),
    0x207: (4, True),
    0x208: (4, False),
    0x20B: (8, True),
    0x20C: (8, False),
}
LLMHF_INJECTED = 0x01
TICK_MASK = 0xFFFFFFFF
SIDE_NEUTRAL = {0xA0: 0x10, 0xA1: 0x10, 0xA2: 0x11, 0xA3: 0x11, 0xA4: 0x12, 0xA5: 0x12}


class HookEngine:
    """Own consumed keys and clicks from press to physical release, and decide everything else.

    Que's shortcuts start with `QUE_KEY`, which never reaches other apps: every modifier
    pressed after it is held back, and every key pressed while it is held is consumed,
    whether or not it is one of Que's shortcuts. Modifiers still held back when another key
    arrives without `QUE_KEY` are replayed with it intact. All other modifiers pass
    through at once. While Que's input is in typing mode every ordinary key and modifier
    is consumed for Que except the system, function and device keys the user expects to
    keep working.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.monitor: InputMonitor | None = None
        self.keyboard_handle: int | None = None
        self.mouse_handle: int | None = None
        self.window_visible = False
        self.input_armed = False
        self.routing_enabled = True
        self.quit_pending = False
        self.press_seq = 0
        self.regions: tuple[tuple[int, int, int, int, int], ...] = ()
        self.held_vk_codes: set[int] = set()  # Non-modifier keys physically held
        self.suppressed_vk_codes: set[int] = set()  # Non-modifier presses owned by Que
        self.input_vk_codes: set[int] = set()  # Owned presses delivered to Que's input
        self.held_modifier_vks: set[int] = set()  # Modifiers physically held
        # Modifier presses intercepted but not yet delivered: VK -> (scan code, flags)
        self.suppressed_mod_events: dict[int, tuple[int, int]] = {}
        self.repeat_hotkeys: dict[int, tuple[int, Callable[[], None]]] = {}
        self.consumed_buttons: dict[
            int, int
        ] = {}  # Mouse presses owned by Que -> window
        self.delivered_buttons: set[int] = set()  # Mouse presses another app received
        self.hover_target = 0
        self.always_active_hotkeys: dict[
            tuple[int, int], tuple[Callable[[], None], bool]
        ] = {}
        self.main_window_hotkeys: dict[
            tuple[int, int], tuple[Callable[[], None], bool]
        ] = {}
        self._set_hotkeys()

    def apply(self, message: dict[str, Any]) -> None:
        """Apply one state update from Que.

        Args:
            message (dict[str, Any]): The update; its "type" names the state that changed.
        """
        kind = message.get("type")
        if kind == "visible":
            self.window_visible = bool(message["value"])
            if not self.window_visible:
                self._set_armed(False, notify=False)
        elif kind == "armed":
            # A response to an older press must not override a newer press's decision
            if int(message["seq"]) >= self.press_seq:
                self._set_armed(bool(message["value"]), notify=False)
        elif kind == "routing":
            self.routing_enabled = bool(message["value"])
            if not self.routing_enabled:
                self._set_armed(False, notify=False)
        elif kind == "regions":
            self.regions = tuple(
                (int(h), int(left), int(top), int(right), int(bottom))
                for h, left, top, right, bottom in message["value"]
            )
        elif kind == "quit":
            self.request_quit()

    def release_all_state(self) -> None:
        """Stop claiming new input when Que disconnects, while still owning held presses."""
        self.window_visible = False
        self.routing_enabled = False
        self.regions = ()
        self._set_armed(False, notify=False)

    def drained(self) -> bool:
        """Report whether no consumed key, buffered modifier or owned click is still held.

        Returns:
            bool: Whether every press Que owns has been physically released.
        """
        return not (
            self.suppressed_vk_codes
            or self.suppressed_mod_events
            or self.held_modifier_vks
            or self.consumed_buttons
        )

    def request_quit(self) -> None:
        """Defer application shutdown until all swallowed physical input is released."""
        self.quit_pending = True
        self._finish_quit_if_released()

    def _low_level_keyboard_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        """Contain every failure and retain ownership through physical release.

        Args:
            nCode (int): The native hook code.
            wParam (int): The keyboard message.
            lParam (int): The low-level keyboard structure address.

        Returns:
            int: One for consumed input, otherwise the next hook's result.
        """
        try:
            return self._process_key(nCode, wParam, lParam)
        except BaseException as error:  # noqa: BLE001  # Native callbacks must never unwind into Windows.
            self._report(f"Keyboard hook error: {error}")
            # Never release an event whose press may belong to Que after a failure
            return 1 if nCode >= 0 else 0

    def _mouse_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        """Route clicks and wheels over Que, leaving pointer motion visible to the foreground app.

        Args:
            nCode (int): The native hook code.
            wParam (int): The mouse message.
            lParam (int): The low-level mouse structure address.

        Returns:
            int: One for consumed input, otherwise the next hook's result.
        """
        try:
            return self._process_pointer(nCode, wParam, lParam)
        except BaseException as error:  # noqa: BLE001  # Native callbacks must never unwind into Windows.
            self._report(f"Mouse hook error: {error}")
            return 1 if self.consumed_buttons and wParam != WM_MOUSEMOVE else 0

    def _set_hotkeys(self) -> None:
        """Populate the hotkey lookup tables from the reviewed shortcut contract."""
        self.always_active_hotkeys = {}
        self.main_window_hotkeys = {}
        for mask, vk, name, repeat, always in HOTKEYS:
            table = self.always_active_hotkeys if always else self.main_window_hotkeys

            def callback(name: str = name) -> None:
                """Run this table entry's action; the default records which action it is."""
                self._on_hotkey(name)

            table[(mask, vk)] = (callback, repeat)

    def _on_hotkey(self, name: str) -> None:
        """Ask Que to run the named shortcut action.

        Args:
            name (str): The shortcut action name.
        """
        self.emit({"type": "action", "name": name})

    def _process_key(self, code: int, message: int, address: int) -> int:
        """Route physical keys while keeping delivered and consumed presses separate.

        Args:
            code (int): The native hook code.
            message (int): The native keyboard message.
            address (int): The low-level keyboard structure address.

        Returns:
            int: One for consumed input, otherwise the next hook's result.
        """
        if code < 0 or message not in (
            WM_KEYDOWN,
            WM_SYSKEYDOWN,
            WM_KEYUP,
            WM_SYSKEYUP,
        ):
            return self._next_keyboard(code, message, address)
        kb = ctypes.cast(address, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if kb.flags & LLKHF_INJECTED:
            return self._next_keyboard(code, message, address)
        vk = int(kb.vkCode)
        down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if self.monitor is not None:
            self._observe(
                key_identity(
                    vk, int(kb.scanCode), bool(kb.flags & LLKHF_EXTENDED), down
                )
            )
        if vk in CHORD_BITS:
            if down:
                if vk in self.held_modifier_vks:
                    # Autorepeat follows its press: withheld stays withheld, delivered passes
                    if vk in self.suppressed_mod_events:
                        return 1
                    return self._next_keyboard(code, message, address)
                self.held_modifier_vks.add(vk)
                if (
                    vk == QUE_KEY
                    or QUE_KEY in self.suppressed_mod_events
                    or self.input_armed
                ):
                    self.suppressed_mod_events[vk] = (int(kb.scanCode), int(kb.flags))
                    return 1
                return self._next_keyboard(code, message, address)
            else:
                self.held_modifier_vks.discard(vk)
                if vk in self.suppressed_mod_events:
                    del self.suppressed_mod_events[vk]
                    self._finish_quit_if_released()
                    return 1
                self._finish_quit_if_released()
            return self._next_keyboard(code, message, address)
        repeated = vk in self.held_vk_codes
        modifiers = self._compute_modifier_mask()
        if not down:
            self.held_vk_codes.discard(vk)
            self.repeat_hotkeys.pop(vk, None)
            if vk in self.suppressed_vk_codes:
                self.suppressed_vk_codes.remove(vk)
                if vk in self.input_vk_codes:
                    self.input_vk_codes.remove(vk)
                    self._emit_key(kb, False, False)
                self._finish_quit_if_released()
                return 1
            return self._next_keyboard(code, message, address)
        if vk in self.suppressed_vk_codes:
            # Ownership survives visibility and modifier changes; only an unchanged
            # repeatable chord may invoke its action again
            repeat = self.repeat_hotkeys.get(vk)
            if repeat is not None and repeat[0] == modifiers and self.window_visible:
                self._invoke(repeat[1])
            elif vk in self.input_vk_codes:
                self._emit_key(kb, True, True)
            return 1
        self.held_vk_codes.add(vk)
        if not repeated:
            windows_held = bool(self.held_vk_codes & WINDOWS_KEYS)
            entry = None
            if not windows_held:
                entry = self.always_active_hotkeys.get((modifiers, vk))
                if entry is None and self.window_visible:
                    entry = self.main_window_hotkeys.get((modifiers, vk))
            if entry is not None:
                callback, repeat_callbacks = entry
                self.suppressed_vk_codes.add(vk)
                if repeat_callbacks:
                    self.repeat_hotkeys[vk] = (modifiers, callback)
                self._invoke(callback)
                return 1
            if QUE_KEY in self.suppressed_mod_events:
                # Que's key never reaches other apps, so neither does a key pressed with it
                self.suppressed_vk_codes.add(vk)
                return 1
            if self.input_armed:
                system = (
                    windows_held
                    or vk in WINDOWS_KEYS
                    or (modifiers, vk) in SYSTEM_CHORDS
                )
                if system:
                    self._set_armed(False, notify=True)
                elif vk not in DEVICE_KEYS:
                    self.suppressed_vk_codes.add(vk)
                    self.input_vk_codes.add(vk)
                    self._emit_key(kb, True, False)
                    return 1
        if self.suppressed_mod_events and QUE_KEY not in self.suppressed_mod_events:
            self._replay_modifiers_and_key(vk, int(kb.scanCode), int(kb.flags))
            return 1
        return self._next_keyboard(code, message, address)

    def _process_pointer(self, code: int, message: int, address: int) -> int:
        """Route clicks and wheels while keeping consumed presses paired.

        Args:
            code (int): The native hook code.
            message (int): The native mouse message.
            address (int): The low-level mouse structure address.

        Returns:
            int: One for consumed input, otherwise the next hook's result.
        """
        if code < 0:
            return self._next_mouse(code, message, address)
        data = ctypes.cast(address, ctypes.POINTER(_MouseData)).contents
        if data.flags & LLMHF_INJECTED:
            return self._next_mouse(code, message, address)
        x, y = int(data.pt.x), int(data.pt.y)
        target = (
            next(
                (
                    hwnd
                    for hwnd, left, top, right, bottom in self.regions
                    if left <= x < right and top <= y < bottom
                ),
                0,
            )
            if self.routing_enabled
            else 0
        )
        button, down = BUTTON_MESSAGES.get(message, (0, False))
        if button == 8:
            button = 8 if data.mouseData >> 16 == 1 else 16
        if self.monitor is not None and (
            button or message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL)
        ):
            self._observe(
                frozenset({("button", button, down) if button else ("wheel", message)})
            )
        if button:
            if down:
                self.press_seq += 1
                if target and button not in self.delivered_buttons:
                    self.consumed_buttons[button] = target
                    self.input_armed = True
                else:
                    self.delivered_buttons.add(button)
                    self.input_armed = False
            else:
                self.delivered_buttons.discard(button)
            owned = self.consumed_buttons.get(button, 0)
            self._emit_pointer(message, x, y, int(data.mouseData), owned or target)
            if owned:
                if not down:
                    self.consumed_buttons.pop(button, None)
                    self._finish_quit_if_released()
                return 1
        elif message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            if target:
                self._emit_pointer(message, x, y, int(data.mouseData), target)
                return 1
        elif message == WM_MOUSEMOVE:
            drag_target = next(iter(self.consumed_buttons.values()), target)
            if drag_target or self.hover_target:
                self._emit_pointer(message, x, y, int(data.mouseData), drag_target)
            self.hover_target = target
        return self._next_mouse(code, message, address)

    def _next_keyboard(self, code: int, message: int, address: int) -> int:
        """Pass a keyboard event to the next hook.

        Args:
            code (int): The native hook code.
            message (int): The keyboard message.
            address (int): The low-level keyboard structure address.

        Returns:
            int: The next hook's result.
        """
        return int(user32.CallNextHookEx(self.keyboard_handle, code, message, address))

    def _next_mouse(self, code: int, message: int, address: int) -> int:
        """Pass a mouse event to the next hook.

        Args:
            code (int): The native hook code.
            message (int): The mouse message.
            address (int): The low-level mouse structure address.

        Returns:
            int: The next hook's result.
        """
        return int(user32.CallNextHookEx(self.mouse_handle, code, message, address))

    def _set_armed(self, armed: bool, notify: bool) -> None:
        """Change whether new ordinary key presses belong to Que's input.

        Args:
            armed (bool): Whether Que's input is in typing mode.
            notify (bool): Whether Que must be told because the hook made the decision.
        """
        if notify and armed != self.input_armed:
            self.emit({"type": "armed", "value": armed, "seq": self.press_seq})
        self.input_armed = armed

    def _invoke(self, callback: Callable[[], None]) -> None:
        """Report an action failure without changing the consumed press's ownership.

        Args:
            callback (Callable[[], None]): The matched shortcut action.
        """
        try:
            callback()
        except BaseException as error:  # noqa: BLE001  # Native callbacks must never unwind into Windows.
            self._report(f"Shortcut failed: {error}")

    def _report(self, text: str) -> None:
        """Send an error to Que without letting a reporting failure escape the hook.

        Args:
            text (str): The error description.
        """
        try:
            self.emit({"type": "error", "text": text})
        except BaseException:  # noqa: BLE001, S110  # Last-resort native callback boundary.
            pass

    def _emit_key(self, kb: KBDLLHOOKSTRUCT, down: bool, repeated: bool) -> None:
        """Send immutable physical key state to Que for layout translation and editing.

        Args:
            kb (KBDLLHOOKSTRUCT): The physical key event.
            down (bool): Whether the key is pressed.
            repeated (bool): Whether this is an autorepeat.
        """
        self.emit(
            {
                "type": "key",
                "vk": int(kb.vkCode),
                "scan": int(kb.scanCode),
                "flags": int(kb.flags),
                "down": down,
                "repeated": repeated,
                "held": sorted(self.held_modifier_vks),
            }
        )

    def _emit_pointer(self, message: int, x: int, y: int, data: int, hwnd: int) -> None:
        """Send a routed pointer event to Que.

        Args:
            message (int): The native mouse message.
            x (int): The physical desktop x coordinate.
            y (int): The physical desktop y coordinate.
            data (int): The wheel delta or extra-button identifier.
            hwnd (int): The hit or owning Que window, or zero outside Que.
        """
        self.emit(
            {
                "type": "pointer",
                "message": message,
                "x": x,
                "y": y,
                "data": data,
                "hwnd": hwnd,
                "seq": self.press_seq,
                "held": sorted(self.held_modifier_vks),
            }
        )

    def _observe(self, key: frozenset[tuple[Any, ...]]) -> None:
        """Record that the hook saw a physical event, for comparison with Raw Input.

        Args:
            key (frozenset[tuple[Any, ...]]): The physical event's identities.
        """
        assert self.monitor is not None
        self.monitor.record_hook(key, int(kernel32.GetTickCount()))

    def _finish_quit_if_released(self) -> None:
        """Tell Que to quit only when every consumed key, buffered modifier and click has drained."""
        if self.quit_pending and self.drained():
            self.quit_pending = False
            self.emit({"type": "quit_ready"})

    def _compute_modifier_mask(self) -> int:
        """Return a bitmask of physically held modifiers from internal tracking.

        Returns:
            int: Bitmask of held modifiers (MOD_QUE, MOD_CTRL, MOD_ALT, MOD_SHIFT).
        """
        mask = 0
        for vk in self.held_modifier_vks:
            mask |= CHORD_BITS.get(vk, 0)
        return mask

    def _replay_modifiers_and_key(
        self, vk_code: int, scan_code: int, flags: int
    ) -> None:
        """Replay a non-Que key combination intact to the foreground application.

        The suppressed modifier records are cleared after injection so their real
        releases pass through normally.

        Args:
            vk_code (int): Virtual key code of the non-modifier key to replay.
            scan_code (int): Hardware scan code of the non-modifier key.
            flags (int): Hook flags from KBDLLHOOKSTRUCT (used to detect extended keys).
        """
        for mod_vk, (mod_scan, mod_flags) in self.suppressed_mod_events.items():
            kb_flags = KEYEVENTF_EXTENDEDKEY if (mod_flags & LLKHF_EXTENDED) else 0
            user32.keybd_event(mod_vk, mod_scan, kb_flags, None)
        self.suppressed_mod_events.clear()
        kb_flags = KEYEVENTF_EXTENDEDKEY if (flags & LLKHF_EXTENDED) else 0
        user32.keybd_event(vk_code, scan_code, kb_flags, None)


def key_identity(
    vk: int, scan: int, extended: bool, down: bool
) -> frozenset[tuple[Any, ...]]:
    """Describe a physical key press or release by scan code and by side-neutral virtual key.

    Raw Input and the low-level hook can disagree on one of the two (numpad keys under
    Num Lock, media keys without a scan code), so a match on either counts.

    Args:
        vk (int): The virtual key code.
        scan (int): The hardware scan code, or zero when unknown.
        extended (bool): Whether the key has the E0 extended prefix.
        down (bool): Whether the key is pressed.

    Returns:
        frozenset[tuple[Any, ...]]: The event's identities.
    """
    identities: set[tuple[Any, ...]] = {("vk", SIDE_NEUTRAL.get(vk, vk), down)}
    if scan & 0xFF:
        identities.add(("scan", scan & 0xFF, extended, down))
    return frozenset(identities)


class InputMonitor:
    """Match hook observations against Raw Input to detect input Windows delivered without the hook.

    Raw Input reaches Que's helper even when Windows has timed out or removed a
    low-level hook, so a physical event seen only by Raw Input means the hook
    missed it and the event reached the foreground application.
    """

    def __init__(self, grace_ms: int = 300, memory_ms: int = 2000) -> None:
        self.grace_ms = grace_ms
        self.memory_ms = memory_ms
        self.hook_seen: deque[tuple[frozenset[tuple[Any, ...]], int]] = deque()
        self.raw_pending: deque[tuple[frozenset[tuple[Any, ...]], int, str]] = deque()
        self.raw_events = 0  # Physical events Raw Input reported
        self.missed_events = 0  # Of those, events the hooks never saw

    def record_hook(self, key: frozenset[tuple[Any, ...]], tick: int) -> None:
        """Record an event the hook observed.

        Args:
            key (frozenset[tuple[Any, ...]]): The physical event's identities.
            tick (int): The current system tick count.
        """
        for index, (pending, _tick, _name) in enumerate(self.raw_pending):
            if pending & key:
                del self.raw_pending[index]
                return
        self.hook_seen.append((key, tick))

    def record_raw(self, key: frozenset[tuple[Any, ...]], tick: int, name: str) -> None:
        """Record an event Raw Input observed.

        Args:
            key (frozenset[tuple[Any, ...]]): The physical event's identities.
            tick (int): The current system tick count.
            name (str): A readable description for the user if the hook missed it.
        """
        self.raw_events += 1
        for index, (seen, _tick) in enumerate(self.hook_seen):
            if seen & key:
                del self.hook_seen[index]
                return
        self.raw_pending.append((key, tick, name))

    def expire(self, tick: int) -> list[str]:
        """Return descriptions of physical events the hook never observed.

        Args:
            tick (int): The current system tick count.

        Returns:
            list[str]: The missed events, oldest first.
        """
        missed = []
        while (
            self.raw_pending
            and (tick - self.raw_pending[0][1]) & TICK_MASK > self.grace_ms
        ):
            missed.append(self.raw_pending.popleft()[2])
        self.missed_events += len(missed)
        while (
            self.hook_seen
            and (tick - self.hook_seen[0][1]) & TICK_MASK > self.memory_ms
        ):
            self.hook_seen.popleft()
        return missed


class _MouseData(ctypes.Structure):
    """Represent a pointer-sized MSLLHOOKSTRUCT."""

    _fields_ = [
        ("pt", ctypes.wintypes.POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("extra", ctypes.c_size_t),
    ]
