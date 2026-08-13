import threading
import time
from typing import Any

import Quartz
from CoreFoundation import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    kCFRunLoopCommonModes,
)

from core.platform.keyboard_hook_base import (
    HotkeyHost,
    Key,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
)


# Carbon HIToolbox ANSI hardware keycodes (kVK_* from <Carbon/HIToolbox/Events.h>).
# These values are fixed for the standard keyboard layout and are not exposed as
# PyObjC symbols, so they are defined here explicitly.
_KVK_ANSI_D = 0x02
_KVK_ANSI_E = 0x0E
_KVK_ANSI_G = 0x05
_KVK_ANSI_N = 0x2D
_KVK_ANSI_Q = 0x0C
_KVK_ANSI_S = 0x01
_KVK_LEFT_ARROW = 0x7B
_KVK_RIGHT_ARROW = 0x7C
_KVK_DOWN_ARROW = 0x7D
_KVK_UP_ARROW = 0x7E

# Modifier hardware keycodes (left and right variants)
_KVK_SHIFT = 0x38
_KVK_RIGHT_SHIFT = 0x3C
_KVK_CONTROL = 0x3B
_KVK_RIGHT_CONTROL = 0x3E
_KVK_OPTION = 0x3A
_KVK_RIGHT_OPTION = 0x3D

# Map macOS hardware keycodes to platform-neutral key tokens for the keys used in hotkeys
MAC_KEYCODE_TO_KEY: dict[int, Key] = {
    _KVK_ANSI_D: Key.D,
    _KVK_ANSI_E: Key.E,
    _KVK_ANSI_G: Key.G,
    _KVK_ANSI_N: Key.N,
    _KVK_ANSI_Q: Key.Q,
    _KVK_ANSI_S: Key.S,
    _KVK_LEFT_ARROW: Key.LEFT,
    _KVK_RIGHT_ARROW: Key.RIGHT,
    _KVK_UP_ARROW: Key.UP,
    _KVK_DOWN_ARROW: Key.DOWN,
}

# Map modifier hardware keycodes to the platform-neutral modifier bitmask values
MODIFIER_KEYCODE_TO_MOD_BIT: dict[int, int] = {
    _KVK_SHIFT: MOD_SHIFT,
    _KVK_RIGHT_SHIFT: MOD_SHIFT,
    _KVK_CONTROL: MOD_CTRL,
    _KVK_RIGHT_CONTROL: MOD_CTRL,
    _KVK_OPTION: MOD_ALT,
    _KVK_RIGHT_OPTION: MOD_ALT,
}

# Map platform-neutral modifier bits to CGEventFlags masks for replayed events
MOD_BIT_TO_CG_FLAG: dict[int, int] = {
    MOD_CTRL: Quartz.kCGEventFlagMaskControl,
    MOD_ALT: Quartz.kCGEventFlagMaskAlternate,
    MOD_SHIFT: Quartz.kCGEventFlagMaskShift,
}

# Marker stored in kCGEventSourceUserData to identify events replayed by Que
INJECTED_EVENT_MARKER = 0x51554521  # "QUE!"


class MacKeyboardHook:
    """Install a Quartz CGEventTap and drive the shortcut manager's hotkeys.

    Key suppression strategy
    ------------------------
    Mirrors the Windows low-level hook: modifier keys (Ctrl, Alt, Shift) are
    intercepted and suppressed the moment they are pressed down, before any
    other application sees them. Their state is tracked internally. When the
    user then presses a non-modifier key, the complete combination (held
    modifiers + that key) is checked against the registered Que hotkeys:

    - Match: the hotkey fires and the entire combination (modifiers + key) is
      fully suppressed — no other application ever sees any part of it.
    - No match: the suppressed modifier flags-changed events are replayed,
      followed by a copy of the key event, so the target application receives
      the complete, intact key combination as if Que had never intercepted it.

    The Command key is never intercepted so system shortcuts stay untouched.
    """

    # Seconds to wait between attempts to create the tap while Accessibility is ungranted
    _RETRY_INTERVAL_SECONDS = 2.0

    def __init__(self, host: HotkeyHost) -> None:
        self.host = host
        self.held_keycodes: set[int] = (
            set()
        )  # Non-modifier keys currently physically held down
        self.suppressed_keycodes: set[int] = (
            set()
        )  # Non-modifier keys whose key-down was swallowed by a matched hotkey
        self.held_modifier_keycodes: set[int] = (
            set()
        )  # Modifier keys currently held (tracked internally)
        self.suppressed_mod_keycodes: set[int] = (
            set()
        )  # Modifier key-downs intercepted but not yet delivered to other applications
        self.stopping = False
        self.tap: Any = None
        self.run_loop: Any = None
        self.run_loop_source: Any = None

    def start(self) -> None:
        """Start the event tap on a dedicated daemon thread."""
        thread = threading.Thread(target=self._tap_thread_entry, daemon=True)
        thread.start()

    def stop(self) -> None:
        """Disable the event tap and stop its run loop."""
        self.stopping = True
        if self.tap is not None:
            Quartz.CGEventTapEnable(self.tap, False)
        if self.run_loop is not None:
            CFRunLoopStop(self.run_loop)
            self.run_loop = None

    def _tap_thread_entry(self) -> None:
        """Create the event tap and run its run loop until stopped.

        Retry creating the tap until it succeeds, because Accessibility
        permission may not be effective the moment the app starts (the user may
        grant it after launch). Once created, run a CFRunLoop on this thread to
        keep the tap alive; CFRunLoopStop (from stop()) causes CFRunLoopRun to
        return.
        """
        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        )

        warned = False
        while not self.stopping:
            self.tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                mask,
                self._tap_callback,
                None,
            )
            if self.tap is not None:
                break
            if not warned:
                # Accessibility permission not yet granted; keep retrying so the
                # hook comes alive once the user grants it, without needing a restart.
                print(
                    "Warning: CGEventTapCreate failed. Grant Accessibility permission "
                    "in System Settings > Privacy & Security > Accessibility. Retrying..."
                )
                warned = True
            time.sleep(self._RETRY_INTERVAL_SECONDS)

        if self.tap is None:
            return  # stop() was called before the tap could be created

        self.run_loop_source = CFMachPortCreateRunLoopSource(None, self.tap, 0)
        self.run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self.run_loop, self.run_loop_source, kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        CFRunLoopRun()

    def _tap_callback(
        self, proxy: Any, event_type: int, event: Any, _refcon: Any
    ) -> Any:
        """Handle one tapped event, returning the event to pass or None to swallow.

        Implements the modifier suppression strategy described in the class
        docstring. The decision tree for each incoming event is:

        1. Injected event (from our own replay) — pass straight through to
           avoid re-processing.
        2. Modifier flags-changed — suppress the press and record it
           internally; suppress the release too if its press was suppressed.
        3. Non-modifier key-down with a matching hotkey — fire the callback and
           suppress the key; the entire combination stays hidden from other
           applications.
        4. Non-modifier key-down with no matching hotkey — replay all
           previously suppressed modifier presses followed by a copy of this
           key event, delivering the intact combination to other applications.
        5. Non-modifier key-up for a suppressed key — suppress the release to
           match its suppressed key-down.

        Args:
            proxy (CGEventTapProxy): The tap proxy used to post replayed events.
            event_type (int): The CGEventType of the event.
            event (CGEventRef): The tapped event.

        Returns:
            CGEventRef: The event to pass through, or None to suppress it.
        """
        # The system disables a tap that is slow or interrupted; re-enable and pass through.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            if self.tap is not None:
                Quartz.CGEventTapEnable(self.tap, True)
            return event

        # Step 1: Pass through events we replayed ourselves (see _replay_modifiers_and_key)
        if (
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
            == INJECTED_EVENT_MARKER
        ):
            return event

        # Step 2: Intercept modifier state changes so no partial combination
        # (e.g. a bare Ctrl press) leaks through to other applications.
        if event_type == Quartz.kCGEventFlagsChanged:
            return self._handle_flags_changed(event)

        if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
            return event

        keycode = int(
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        )
        key = MAC_KEYCODE_TO_KEY.get(keycode)
        is_key_down = event_type == Quartz.kCGEventKeyDown

        if not is_key_down:
            # Step 5: Key-up — swallow it if its key-down was swallowed, regardless of
            # the live modifier or visibility state (which may have changed since the
            # key-down). This keeps events balanced so no unpaired key-up leaks to
            # other applications, matching the Windows key-up suppression.
            self.held_keycodes.discard(keycode)
            if keycode in self.suppressed_keycodes:
                self.suppressed_keycodes.discard(keycode)
                return None
            return event

        # Non-modifier key-down: now we have the full picture — check the combination
        # against the hotkey tables using the internally tracked modifier state.
        modifiers = self._compute_modifier_mask()
        entry = self.host.resolve_hotkey(modifiers, key) if key is not None else None

        if entry is None:
            if self.suppressed_mod_keycodes:
                # Step 4: No hotkey match — replay the suppressed modifier presses
                # followed by this key so other applications receive the intact combination.
                self._replay_modifiers_and_key(proxy, event)
                return (
                    None  # Suppress the original event; the injected replay delivers it
                )
            return event

        # Step 3: Que hotkey matched — fire the callback and suppress the entire
        # combination; the suppressed modifier presses are never replayed.
        callback, repeat_callbacks = entry
        is_autorepeat = bool(
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
        )
        # For no-repeat hotkeys, ignore OS auto-repeat and physically-held repeats
        if not repeat_callbacks and (is_autorepeat or keycode in self.held_keycodes):
            return None  # Suppress repeat without calling callback
        self.held_keycodes.add(keycode)
        self.suppressed_keycodes.add(keycode)
        callback()
        return None  # Suppress the key event

    def _handle_flags_changed(self, event: Any) -> Any:
        """Suppress and track a modifier state change, mirroring the Windows hook.

        A press is suppressed and recorded so the decision of whether to
        ultimately suppress or replay it is deferred until the accompanying
        non-modifier key arrives. A release is suppressed only if its press is
        still suppressed (the press was never delivered, so neither should its
        release be).

        Args:
            event (CGEventRef): The flags-changed event.

        Returns:
            CGEventRef: The event to pass through, or None to suppress it.
        """
        keycode = int(
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        )
        mod_bit = MODIFIER_KEYCODE_TO_MOD_BIT.get(keycode)
        if mod_bit is None:
            return event  # Command, Fn, Caps Lock, etc. are never intercepted

        if keycode in self.held_modifier_keycodes:
            # Release of a tracked press
            self.held_modifier_keycodes.discard(keycode)
            if keycode in self.suppressed_mod_keycodes:
                self.suppressed_mod_keycodes.discard(keycode)
                return None  # Suppress the release to match the suppressed press
            return event

        if Quartz.CGEventGetFlags(event) & MOD_BIT_TO_CG_FLAG[mod_bit]:
            # Press
            self.held_modifier_keycodes.add(keycode)
            self.suppressed_mod_keycodes.add(keycode)
            return None  # Suppress the modifier press

        return event  # Release of a modifier held before the tap started

    def _compute_modifier_mask(self) -> int:
        """Return a bitmask of currently held modifier keys based on internal tracking.

        Uses the internally maintained set of held modifier keycodes rather than
        the event's flags, because all modifier events are suppressed before
        other applications can observe them.

        Returns:
            int: Bitmask of active modifier keys (MOD_CTRL, MOD_ALT, MOD_SHIFT).
        """
        mask = 0
        for keycode in self.held_modifier_keycodes:
            mask |= MODIFIER_KEYCODE_TO_MOD_BIT[keycode]
        return mask

    def _replay_modifiers_and_key(self, proxy: Any, event: Any) -> None:
        """Replay a non-Que key combination to other applications.

        Posts the previously suppressed modifier presses as flags-changed
        events followed by a copy of the non-modifier key event, reconstructing
        the full key combination as if Que had never intercepted it. The
        suppressed modifier records are cleared after injection so that their
        real release events (which continue to arrive at the tap) are no longer
        suppressed and pass through normally.

        The injected events carry a marker in kCGEventSourceUserData, which the
        tap callback uses to identify and pass them through without
        re-processing (step 1).

        Args:
            proxy (CGEventTapProxy): The tap proxy the events are posted through.
            event (CGEventRef): The original non-modifier key event to replay.
        """
        flags = 0
        for keycode in sorted(self.suppressed_mod_keycodes):
            flags |= MOD_BIT_TO_CG_FLAG[MODIFIER_KEYCODE_TO_MOD_BIT[keycode]]
            mod_event = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
            Quartz.CGEventSetType(mod_event, Quartz.kCGEventFlagsChanged)
            Quartz.CGEventSetFlags(mod_event, flags)
            Quartz.CGEventSetIntegerValueField(
                mod_event, Quartz.kCGEventSourceUserData, INJECTED_EVENT_MARKER
            )
            Quartz.CGEventTapPostEvent(proxy, mod_event)
        self.suppressed_mod_keycodes.clear()

        key_event = Quartz.CGEventCreateCopy(event)
        Quartz.CGEventSetIntegerValueField(
            key_event, Quartz.kCGEventSourceUserData, INJECTED_EVENT_MARKER
        )
        Quartz.CGEventTapPostEvent(proxy, key_event)
