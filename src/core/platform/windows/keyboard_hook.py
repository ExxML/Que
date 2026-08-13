import ctypes
import ctypes.wintypes
import threading

from core.platform.keyboard_hook_base import (
    HotkeyHost,
    Key,
)
from core.platform.windows.win32_hook import (
    HOOKPROC,
    KBDLLHOOKSTRUCT,
    KEYEVENTF_EXTENDEDKEY,
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    MODIFIER_VK_CODES,
    VK_DOWN,
    VK_LEFT,
    VK_RIGHT,
    VK_TO_MOD_BIT,
    VK_UP,
    WH_KEYBOARD_LL,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_QUIT,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    kernel32,
    user32,
)


# Map Windows virtual-key codes to platform-neutral key tokens for the keys used in hotkeys
WIN_KEYCODE_TO_KEY: dict[int, Key] = {
    ord("D"): Key.D,
    ord("E"): Key.E,
    ord("G"): Key.G,
    ord("N"): Key.N,
    ord("Q"): Key.Q,
    ord("S"): Key.S,
    VK_LEFT: Key.LEFT,
    VK_RIGHT: Key.RIGHT,
    VK_UP: Key.UP,
    VK_DOWN: Key.DOWN,
}


class WindowsKeyboardHook:
    """Install a Win32 WH_KEYBOARD_LL hook and drive the shortcut manager's hotkeys.

    Key suppression strategy
    ------------------------
    Modifier keys (Ctrl, Alt, Shift) are intercepted and suppressed the moment
    they are pressed down, before any other application sees them. Their state
    is tracked internally. When the user then presses a non-modifier key, the
    hook checks whether the complete combination (held modifiers + that key)
    matches a registered Que hotkey:

    - Match: the hotkey fires and the entire combination (modifiers + key) is
      fully suppressed — no other application ever sees any part of it.
    - No match: the suppressed modifier key-downs are replayed via keybd_event
      so the target application receives the complete, intact key combination
      as if Que had never intercepted it.
    """

    def __init__(self, host: HotkeyHost) -> None:
        self.host = host
        self.suppressed_vk_codes: set[int] = (
            set()
        )  # Non-modifier keys currently suppressed by a matched hotkey
        self.held_vk_codes: set[int] = (
            set()
        )  # Non-modifier keys currently physically held down
        self.held_modifier_vks: set[int] = (
            set()
        )  # Modifier keys currently held (tracked internally, never forwarded to the OS)
        self.suppressed_mod_events: dict[
            int, tuple[int, int]
        ] = {}  # Modifier key-downs intercepted but not yet delivered: VK -> (scanCode, flags)

        # Low-level keyboard hook (prevent GC of the callback reference)
        self.hook_proc_ref = HOOKPROC(self._low_level_keyboard_proc)
        self.hook_handle: int | None = None
        self.hook_thread_id: int | None = None

    def start(self) -> None:
        """Start the keyboard hook on a dedicated daemon thread."""
        hook_thread = threading.Thread(target=self._hook_thread_entry, daemon=True)
        hook_thread.start()

    def stop(self) -> None:
        """Stop the keyboard hook and its message loop."""
        if self.hook_thread_id is not None:
            user32.PostThreadMessageW(self.hook_thread_id, WM_QUIT, 0, 0)
            self.hook_thread_id = None

    def _low_level_keyboard_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        """Win32 low-level keyboard hook callback.

        Implements the modifier suppression strategy described in the class
        docstring. The decision tree for each incoming key event is:

        1. Injected event (from our own keybd_event replay) — pass straight
           through to avoid re-processing.
        2. Modifier key-down — suppress and record internally; do not forward.
        3. Modifier key-up — if the corresponding key-down was suppressed,
           suppress the key-up too (the key was never delivered, so neither
           should its release be).
        4. Non-modifier key-down with a matching hotkey — fire the callback and
           suppress the key; the entire combination stays hidden from the OS.
        5. Non-modifier key-down with no matching hotkey — replay all previously
           suppressed modifier key-downs followed by this key via keybd_event,
           delivering the intact combination to other applications.
        6. Non-modifier key-up for a suppressed key — suppress the release to
           match its suppressed key-down.

        Args:
            nCode (int): Hook code indicating how to process the message.
            wParam (int): Message type identifier (e.g. WM_KEYDOWN, WM_KEYUP).
            lParam (int): Pointer to a KBDLLHOOKSTRUCT containing key event data.

        Returns:
            int: 1 to suppress the key event, or the result of CallNextHookEx.
        """
        if nCode >= 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk_code = kb.vkCode
            is_key_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_key_up = wParam in (WM_KEYUP, WM_SYSKEYUP)

            # Step 1: Pass through events we injected ourselves via keybd_event (see step 5 below)
            if kb.flags & LLKHF_INJECTED:
                return int(
                    user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)
                )

            if vk_code in MODIFIER_VK_CODES:
                # Steps 2 & 3: Intercept all modifier keys so that no partial combination
                # (e.g. a bare Ctrl press) leaks through to other applications. The decision
                # of whether to ultimately suppress or replay is deferred until we see the
                # accompanying non-modifier key.
                if is_key_down:
                    self.held_modifier_vks.add(vk_code)
                    self.suppressed_mod_events[vk_code] = (
                        int(kb.scanCode),
                        int(kb.flags),
                    )
                    return 1  # Step 2: Suppress modifier key-down

                if is_key_up:
                    self.held_modifier_vks.discard(vk_code)
                    if vk_code in self.suppressed_mod_events:
                        del self.suppressed_mod_events[vk_code]
                        return 1  # Step 3: Suppress key-up to match the suppressed key-down
            else:
                # Non-modifier key: now we have the full picture — check the combination
                # against the hotkey tables using the internally tracked modifier state.
                modifiers = self._compute_modifier_mask()
                key = WIN_KEYCODE_TO_KEY.get(vk_code)
                entry = (
                    self.host.resolve_hotkey(modifiers, key)
                    if key is not None
                    else None
                )

                if entry is not None:
                    # Step 4: Que hotkey matched — fire the callback and suppress the
                    # entire combination; the suppressed modifier key-downs are never replayed.
                    callback, repeat_callbacks = entry
                    if is_key_down:
                        # For no-repeat hotkeys, skip if the key is already physically held
                        if not repeat_callbacks and vk_code in self.held_vk_codes:
                            return 1  # Suppress repeat without calling callback
                        self.held_vk_codes.add(vk_code)
                        self.suppressed_vk_codes.add(vk_code)
                        callback()
                        return 1  # Suppress the key event

                    if is_key_up:
                        self.held_vk_codes.discard(vk_code)
                        if vk_code in self.suppressed_vk_codes:
                            self.suppressed_vk_codes.discard(vk_code)
                            return 1  # Suppress the matching key-up

                elif is_key_down and self.suppressed_mod_events:
                    # Step 5: No hotkey match — replay the suppressed modifier key-downs
                    # followed by this key so other applications receive the intact combination.
                    self._replay_modifiers_and_key(
                        int(vk_code), int(kb.scanCode), int(kb.flags)
                    )
                    return 1  # Suppress the original event; the injected replay delivers it

                elif is_key_up:
                    # Step 6: Suppress key-up for any key whose key-down was already suppressed
                    # (handles the edge case where a modifier was released before key-up)
                    self.held_vk_codes.discard(vk_code)
                    if vk_code in self.suppressed_vk_codes:
                        self.suppressed_vk_codes.discard(vk_code)
                        return 1  # Suppress key-up to match its suppressed key-down

        return int(user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam))

    def _compute_modifier_mask(self) -> int:
        """Return a bitmask of currently held modifier keys based on internal tracking.

        Uses the internally maintained set of held modifier VK codes rather than
        querying the OS directly, because all modifier events are suppressed before
        the OS can update its own key-state registers.

        Returns:
            int: Bitmask of active modifier keys (MOD_CTRL, MOD_ALT, MOD_SHIFT).
        """
        mask = 0
        for vk in self.held_modifier_vks:
            mask |= VK_TO_MOD_BIT.get(vk, 0)
        return mask

    def _replay_modifiers_and_key(
        self, vk_code: int, scan_code: int, flags: int
    ) -> None:
        """Replay a non-Que key combination to other applications.

        Injects the previously suppressed modifier key-downs followed by the
        non-modifier key into the input stream via keybd_event, reconstructing
        the full key combination as if Que had never intercepted it. The
        suppressed modifier records are cleared after injection so that their
        real key-up events (which continue to arrive from the hook) are no
        longer suppressed and pass through normally.

        The injected events carry the LLKHF_INJECTED flag, which the hook uses
        to identify and pass them through without re-processing (step 1).

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

    def _hook_thread_entry(self) -> None:
        """Install the keyboard hook and run its message loop.

        Keep the hook alive until a WM_QUIT message is posted.
        """
        self.hook_thread_id = kernel32.GetCurrentThreadId()
        h_module = kernel32.GetModuleHandleW(None)

        self.hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self.hook_proc_ref,
            h_module,
            0,  # Monitor all threads (required for low-level hooks)
        )

        # Pump messages to keep the hook alive
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass  # No dispatch needed; WM_QUIT causes GetMessageW to return 0

        if self.hook_handle:
            user32.UnhookWindowsHookEx(self.hook_handle)
            self.hook_handle = None
