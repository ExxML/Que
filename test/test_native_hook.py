"""Require the compiled hook engine to decide exactly like the reference Python HookEngine."""

import ctypes
import importlib.util
import itertools
import random
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from stealth.hooks import EXTENDED_MODIFIERS, chord_groups

from core.hook_engine import HOTKEYS, HookEngine, _MouseData
from core.native_hook import (
    STATUS_KEYBOARD,
    STATUS_MEMORY_LOCKED,
    STATUS_MOUSE,
    STATUS_PRIORITY,
    NativeHookEngine,
)
from core.win32_hook import (
    KBDLLHOOKSTRUCT,
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
)

MODIFIERS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)
# Hotkey keys, letters, Windows, Tab, Esc, F4, F2, Enter, arrows, lock, media and volume keys
KEYS = (
    *sorted({vk for _, vk, _, _, _ in HOTKEYS}),
    ord("A"),
    ord("W"),
    0x5B,
    0x5C,
    0x09,
    0x1B,
    0x73,
    0x71,
    0x0D,
    0x14,
    0x90,
    0xAF,
    0xB3,
    0x2C,
)
BUTTONS = (
    (0x201, 0x202, 0),
    (0x204, 0x205, 0),
    (0x207, 0x208, 0),
    (0x20B, 0x20C, 1 << 16),
    (0x20B, 0x20C, 2 << 16),
)


class Recorder:
    """Collect what one engine emits and observes."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.observed: list[frozenset[tuple[Any, ...]]] = []

    def record_hook(self, key: frozenset[tuple[Any, ...]], _tick: int) -> None:
        """Record a hook observation, ignoring its timestamp."""
        self.observed.append(key)


class DualDriver:
    """Feed identical input to both engines and fail on the first difference."""

    def __init__(self, test: unittest.TestCase, observe: bool = False) -> None:
        self.test = test
        self.python_out, self.native_out = Recorder(), Recorder()
        self.python = HookEngine(self.python_out.events.append)
        self.native = NativeHookEngine(self.native_out.events.append)
        if observe:
            self.python.monitor = self.python_out  # type: ignore[assignment]
            self.native.monitor = self.native_out  # type: ignore[assignment]
        self.api = MagicMock()
        self.api.CallNextHookEx.return_value = 0
        # Windows re-enters the hook for each injected key before the send returns; mirror it
        self.api.keybd_event.side_effect = self._reenter_python
        self.native.replace_sender(self._reenter_native)
        self.native_replays: list[tuple[int, int, int]] = []
        self.reentry_failures: list[str] = []
        self.steps: list[str] = []

    def close(self) -> None:
        """Free the compiled engine."""
        self.native.close()

    def key(
        self,
        vk: int,
        down: bool = True,
        system: bool = False,
        flags: int = 0,
        code: int = 0,
        scan: int | None = None,
    ) -> int:
        """Decide one keyboard event in both engines and compare everything observable."""
        message = (
            (WM_SYSKEYDOWN if down else WM_SYSKEYUP)
            if system
            else (WM_KEYDOWN if down else WM_KEYUP)
        )
        scan = vk if scan is None else scan
        self.steps.append(
            f"key vk={vk:#x} down={down} system={system} flags={flags:#x} code={code}"
        )
        event = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=flags)
        self.api.keybd_event.reset_mock()
        with patch("core.hook_engine.user32", self.api):
            expected = self.python._low_level_keyboard_proc(
                code, message, ctypes.addressof(event)
            )
        python_replays = [
            (call.args[0], call.args[1], call.args[2])
            for call in self.api.keybd_event.call_args_list
        ]
        self.native_replays = []
        actual = self.native.process_key(code, message, vk, scan, flags)
        self.test.assertEqual(
            self.reentry_failures, [], "an injected replay was not passed through"
        )
        self._compare(expected, actual, python_replays, self.native_replays)
        return actual

    def pointer(
        self, message: int, x: int, y: int, data: int = 0, flags: int = 0, code: int = 0
    ) -> int:
        """Decide one mouse event in both engines and compare everything observable."""
        self.steps.append(
            f"pointer msg={message:#x} at=({x},{y}) data={data:#x} flags={flags} code={code}"
        )
        mouse = _MouseData()
        mouse.pt.x, mouse.pt.y, mouse.mouseData, mouse.flags = x, y, data, flags
        with patch("core.hook_engine.user32", self.api):
            expected = self.python._mouse_proc(code, message, ctypes.addressof(mouse))
        actual = self.native.process_pointer(code, message, x, y, data, flags)
        self._compare(expected, actual, [], [])
        return actual

    def apply(self, message: dict[str, Any]) -> None:
        """Apply one state update from Que to both engines."""
        self.steps.append(f"apply {message}")
        self.python.apply(message)
        self.native.apply(message)
        self._compare(0, 0, [], [])

    def release_all(self) -> None:
        """Simulate Que disconnecting."""
        self.steps.append("release_all")
        self.python.release_all_state()
        self.native.release_all_state()
        self._compare(0, 0, [], [])

    def _reenter_python(self, vk: int, scan: int, flags: int, _extra: Any) -> None:
        """Feed a Python replay back in as the injected event Windows would deliver."""
        event = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=LLKHF_INJECTED | flags)
        if self.python._low_level_keyboard_proc(0, WM_KEYDOWN, ctypes.addressof(event)):
            self.reentry_failures.append(f"python consumed injected vk={vk:#x}")

    def _reenter_native(self, replays: list[tuple[int, int, int]]) -> None:
        """Record native replays and feed each back in, nested, as Windows does inside SendInput."""
        self.native_replays.extend(replays)
        if (
            len(self.native_replays) > 64
        ):  # A replay that re-sends itself would recurse forever
            self.reentry_failures.append("native replays repeat without end")
            return
        for vk, scan, flags in replays:
            if self.native.process_key(0, WM_KEYDOWN, vk, scan, LLKHF_INJECTED | flags):
                self.reentry_failures.append(f"native consumed injected vk={vk:#x}")

    def _compare(
        self,
        expected: int,
        actual: int,
        python_replays: list[tuple[int, int, int]],
        native_replays: list[tuple[int, int, int]],
    ) -> None:
        """Compare decision, replays, emitted events, observations and retained state."""
        python_state = {
            "held_vk_codes": set(self.python.held_vk_codes),
            "suppressed_vk_codes": set(self.python.suppressed_vk_codes),
            "held_modifier_vks": set(self.python.held_modifier_vks),
            "buffered_modifiers": len(self.python.suppressed_mod_events),
            "owned_buttons": len(self.python.consumed_buttons),
            "input_armed": self.python.input_armed,
            "press_seq": self.python.press_seq,
        }
        context = "\n".join(self.steps[-12:])
        self.test.assertEqual(expected, actual, f"decision differs after:\n{context}")
        self.test.assertEqual(
            python_replays, native_replays, f"replays differ after:\n{context}"
        )
        self.test.assertEqual(
            self.python_out.events,
            self.native_out.events,
            f"events differ after:\n{context}",
        )
        self.test.assertEqual(
            self.python_out.observed,
            self.native_out.observed,
            f"observations differ after:\n{context}",
        )
        self.test.assertEqual(
            python_state, self.native.state(), f"state differs after:\n{context}"
        )
        self.test.assertEqual(
            self.python.drained(),
            self.native.drained(),
            f"drained differs after:\n{context}",
        )


class NativeHookEquivalenceTests(unittest.TestCase):
    """Run the offline audit's sequence families and seeded random input through both engines."""

    def driver(self, visible: bool = True, observe: bool = False) -> DualDriver:
        """Create a dual driver with Que's window visibility set."""
        driver = DualDriver(self, observe)
        self.addCleanup(driver.close)
        driver.apply({"type": "visible", "value": visible})
        return driver

    def test_every_hotkey_chord_variant_and_release_order(self) -> None:
        """Mirror the audit's chord families: modifier variants, orders, visibility, early release, system keys."""
        for mask, vk, _name, _repeat, _always in HOTKEYS:
            for variant in range(3):
                modifiers = [group[variant] for group in chord_groups(mask)]
                for order in itertools.permutations(modifiers):
                    for visible, early_release, system in itertools.product(
                        (True, False), repeat=3
                    ):
                        driver = self.driver(visible)
                        for mod in order:
                            driver.key(
                                mod,
                                system=system,
                                flags=LLKHF_EXTENDED
                                if mod in EXTENDED_MODIFIERS
                                else 0,
                            )
                        driver.key(vk, system=system)
                        driver.key(vk, system=system)
                        if early_release:
                            for mod in reversed(order):
                                driver.key(mod, False, system)
                        driver.key(vk, system=system)
                        driver.key(vk, False, system)
                        if not early_release:
                            for mod in reversed(order):
                                driver.key(mod, False, system)

    def test_mixed_modifier_press_and_release_permutations(self) -> None:
        """Mirror the audit's permutation family with repeats after every partial release."""
        for mask, vk, _name, _repeat, _always in HOTKEYS:
            for mixed in itertools.product(*chord_groups(mask)):
                for press_order in itertools.permutations(mixed):
                    for release_order in itertools.permutations((*mixed, vk)):
                        for visible in (True, False):
                            driver = self.driver(visible)
                            for mod in press_order:
                                driver.key(
                                    mod,
                                    flags=LLKHF_EXTENDED
                                    if mod in EXTENDED_MODIFIERS
                                    else 0,
                                )
                            driver.key(vk)
                            driver.key(vk)
                            action_held = True
                            for key in release_order:
                                driver.key(key, False)
                                action_held = action_held and key != vk
                                if action_held:
                                    driver.key(vk)

    def test_seeded_random_input_and_state_changes(self) -> None:
        """Random keys, clicks, wheels and Que state updates, including impossible orders."""
        for seed in range(150):
            rng = random.Random(seed)
            driver = self.driver(rng.random() < 0.7, observe=seed % 2 == 0)
            regions = [(1001, 100, 100, 400, 400), (1002, 500, 100, 700, 300)]
            driver.apply({"type": "regions", "value": regions})
            held: set[int] = set()
            buttons: set[int] = set()
            for _ in range(300):
                roll = rng.random()
                if roll < 0.55:
                    vk = rng.choice((*MODIFIERS, *KEYS))
                    # Mostly physically plausible, sometimes repeats or stray releases
                    down = vk not in held if rng.random() < 0.85 else rng.random() < 0.5
                    (held.add if down else held.discard)(vk)
                    flags = rng.choice((0, 0, 0, LLKHF_EXTENDED, LLKHF_INJECTED))
                    driver.key(
                        vk,
                        down,
                        system=rng.random() < 0.2,
                        flags=flags,
                        code=-1 if rng.random() < 0.02 else 0,
                        scan=rng.choice((vk, 0, 0x1D)),
                    )
                elif roll < 0.8:
                    x, y = rng.choice(
                        (
                            (150, 150),
                            (550, 150),
                            (50, 50),
                            (399, 399),
                            (400, 400),
                            (650, 299),
                        )
                    )
                    kind = rng.random()
                    if kind < 0.4:
                        driver.pointer(0x200, x, y)
                    elif kind < 0.55:
                        driver.pointer(
                            rng.choice((0x20A, 0x20E)),
                            x,
                            y,
                            data=(rng.choice((120, -120)) & 0xFFFF) << 16,
                        )
                    else:
                        index = rng.randrange(len(BUTTONS))
                        down_message, up_message, data = BUTTONS[index]
                        down = (
                            index not in buttons
                            if rng.random() < 0.9
                            else rng.random() < 0.5
                        )
                        (buttons.add if down else buttons.discard)(index)
                        driver.pointer(
                            down_message if down else up_message,
                            x,
                            y,
                            data=data,
                            flags=1 if rng.random() < 0.05 else 0,
                        )
                else:
                    choice = rng.random()
                    if choice < 0.25:
                        driver.apply({"type": "visible", "value": rng.random() < 0.6})
                    elif choice < 0.55:
                        driver.apply(
                            {
                                "type": "armed",
                                "value": rng.random() < 0.7,
                                "seq": max(
                                    0, driver.python.press_seq - rng.choice((0, 0, 1))
                                ),
                            }
                        )
                    elif choice < 0.7:
                        driver.apply({"type": "routing", "value": rng.random() < 0.8})
                    elif choice < 0.82:
                        driver.apply(
                            {
                                "type": "regions",
                                "value": rng.choice((regions, regions[:1], [])),
                            }
                        )
                    elif choice < 0.92:
                        driver.apply({"type": "quit"})
                    else:
                        driver.release_all()

    def test_typing_mode_pass_through_list(self) -> None:
        """Typing mode: characters swallowed; F-keys, media, locks and system chords pass."""
        for keys in (
            (ord("A"),),
            (0x71,),
            (0xAF,),
            (0x14,),
            (0x12, 0x09),
            (0x11, 0x1B),
            (0x5B,),
            (0x12, 0x73),
            (0xA2, ord("V")),
        ):
            driver = self.driver()
            driver.apply({"type": "armed", "value": True, "seq": 0})
            for key in keys:
                driver.key(key)
            for key in reversed(keys):
                driver.key(key, False)

    def test_library_was_rebuilt_after_the_last_source_change(self) -> None:
        """Catch a que_hook.c edit committed without rebuilding src/assets/que_hook.dll."""
        script = Path(__file__).resolve().parents[1] / "native" / "build.py"
        spec = importlib.util.spec_from_file_location("native_build", script)
        assert spec is not None and spec.loader is not None
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        engine = NativeHookEngine(lambda _event: None)
        self.addCleanup(engine.close)
        engine.library.que_source_hash.restype = ctypes.c_char_p
        self.assertEqual(
            engine.library.que_source_hash().decode(),
            build.source_hash(),
            "que_hook.dll is out of date: run python native/build.py and commit the DLL.",
        )

    def test_live_hooks_start_reinstall_and_stop(self) -> None:
        """Install the real hooks on the native thread, reinstall them, and remove them."""
        engine = NativeHookEngine(lambda _event: None)
        self.addCleanup(engine.close)
        status = engine.start(0, 0)
        required = STATUS_KEYBOARD | STATUS_MOUSE | STATUS_PRIORITY
        self.assertEqual(status & required, required, f"status {status:#x}")
        self.assertTrue(
            status & STATUS_MEMORY_LOCKED, f"memory not locked: status {status:#x}"
        )
        started = time.perf_counter()
        status = engine.reinstall()
        self.assertEqual(status & required, required)
        self.assertLess(time.perf_counter() - started, 1.0)
        engine.apply({"type": "quit"})
        deadline = time.monotonic() + 2
        while (
            engine.stats()["status"]
            and time.monotonic() < deadline
            and not engine.drained()
        ):
            time.sleep(0.01)
        self.assertTrue(engine.drained())
        engine.stop()
        self.assertFalse(engine.started)


if __name__ == "__main__":
    unittest.main()
