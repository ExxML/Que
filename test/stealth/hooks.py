"""Exercise the production hook decisions with real structures and mocked Win32 calls."""

import ctypes
import itertools
import random
from typing import Any
from unittest.mock import MagicMock, patch

from core.hook_engine import MOD_QUE, QUE_KEY, HookEngine
from core.hook_host import HookHost
from core.win32_hook import (
    KBDLLHOOKSTRUCT,
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    MOD_ALT,
    MOD_SHIFT,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
)
from stealth.manifest import HOTKEYS
from stealth.results import Result

# Physical keys per chord bit: Que's own key (Right and Left Ctrl as non-Que substitutes),
# Alt and Shift. Variant n of a chord presses the nth key of every group.
CHORD_GROUPS = (
    (MOD_QUE, (QUE_KEY, 0xA3, 0xA2)),
    (MOD_ALT, (0x12, 0xA4, 0xA5)),
    (MOD_SHIFT, (0x10, 0xA0, 0xA1)),
)
EXTENDED_MODIFIERS = frozenset(
    {0xA3, 0xA5}
)  # Right Ctrl and Right Alt carry the E0 prefix


def chord_groups(mask: int) -> list[tuple[int, ...]]:
    """Return the physical key choices for each modifier bit of a chord mask."""
    return [group for bit, group in CHORD_GROUPS if mask & bit]


def delivered_only_before_que(
    driver: "HookDriver", press_order: tuple[int, ...]
) -> bool:
    """Check that only the modifiers pressed before Que's key reached the app, each once and released."""
    before = press_order[: press_order.index(QUE_KEY)]
    downstream = sorted((key, down) for key, down, _ in driver.delivered)
    return downstream == sorted(
        [(key, True) for key in before] + [(key, False) for key in before]
    )


class HookDriver:
    """Invoke the actual callback while recording downstream delivery and actions."""

    def __init__(self, visible: bool = True) -> None:
        self.actions: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.delivered: list[tuple[int, bool, bool]] = []
        self.trace: list[dict[str, Any]] = []
        self.manager = HookEngine(self.observe)
        self.manager.window_visible = visible
        self.window = MagicMock()
        self.window.isVisible.return_value = visible
        self.api = MagicMock()
        self.api.CallNextHookEx.return_value = 0
        self.api.keybd_event.side_effect = self.replay

    def observe(self, event: dict[str, Any]) -> None:
        """Record every event the engine sends to Que, and each shortcut action separately."""
        self.events.append(event)
        if event["type"] == "action":
            self.actions.append(event["name"])

    def replay(self, vk: int, scan: int, flags: int, extra: Any) -> None:
        """Record the OS-visible replay and verify injected callbacks pass through."""
        self.delivered.append((vk, not bool(flags & 2), True))
        event = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan, flags=LLKHF_INJECTED | flags)
        result = self.manager._low_level_keyboard_proc(
            0, WM_KEYDOWN, ctypes.addressof(event)
        )
        if result != 0:
            raise AssertionError("Injected replay did not pass through.")

    def event(
        self,
        vk: int,
        down: bool = True,
        system: bool = False,
        flags: int = 0,
        code: int = 0,
    ) -> int:
        """Deliver one controlled physical event and retain its observable outcome."""
        message = (
            (WM_SYSKEYDOWN if down else WM_SYSKEYUP)
            if system
            else (WM_KEYDOWN if down else WM_KEYUP)
        )
        event = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=vk, flags=flags)
        with patch("core.hook_engine.user32", self.api):
            result = self.manager._low_level_keyboard_proc(
                code, message, ctypes.addressof(event)
            )
        if result != 1:
            self.delivered.append((vk, down, bool(flags & LLKHF_INJECTED)))
        self.trace.append(
            {
                "vk": vk,
                "down": down,
                "system": system,
                "flags": flags,
                "code": code,
                "result": result,
            }
        )
        return result

    def clear(self) -> bool:
        """Check that every internal pressed-key collection has drained."""
        return not any(
            (
                self.manager.held_vk_codes,
                self.manager.suppressed_vk_codes,
                self.manager.held_modifier_vks,
                self.manager.suppressed_mod_events,
            )
        )


def audit_hooks() -> tuple[list[Result], list[dict[str, Any]]]:
    """Run deterministic and seeded compliance checks, retaining genuine failures."""
    results: list[Result] = []
    traces: list[dict[str, Any]] = []

    def record(name: str, driver: HookDriver, ok: bool, reason: str) -> None:
        """Append independent compliance assertions and their complete event traces."""
        results.append(
            Result(
                name,
                "PASS" if ok else "FAIL",
                reason,
                configuration={
                    "input": "controlled callback; not physical",
                    "mocked": "Win32 downstream and actions",
                },
                evidence=["native/hook-traces.json"],
            )
        )
        traces.append(
            {
                "check": name,
                "events": driver.trace,
                "delivered": driver.delivered,
                "actions": driver.actions,
            }
        )

    inventory = HookDriver()
    actual = {
        (mask, vk, (callback.__defaults__ or ("unknown",))[0], repeat, always)
        for always, table in (
            (True, inventory.manager.always_active_hotkeys),
            (False, inventory.manager.main_window_hotkeys),
        )
        for (mask, vk), (callback, repeat) in table.items()
    }
    record(
        "inventory.hotkeys",
        inventory,
        actual == set(HOTKEYS),
        "Compare actual populated tables with all 16 reviewed action contracts.",
    )
    for mask, vk, name, repeat, always in HOTKEYS:
        for variant in range(3):
            modifiers = [group[variant] for group in chord_groups(mask)]
            for order in itertools.permutations(modifiers):
                for visible, early_release, system in itertools.product(
                    (True, False), (True, False), (True, False)
                ):
                    driver = HookDriver(visible)
                    for mod in order:
                        driver.event(
                            mod,
                            system=system,
                            flags=LLKHF_EXTENDED if mod in EXTENDED_MODIFIERS else 0,
                        )
                    driver.event(vk, system=system)
                    driver.event(vk, system=system)
                    if early_release:
                        for mod in reversed(order):
                            driver.event(mod, False, system)
                    driver.event(vk, system=system)
                    driver.event(vk, False, system)
                    if not early_release:
                        for mod in reversed(order):
                            driver.event(mod, False, system)
                    active = QUE_KEY in modifiers and (always or visible)
                    if QUE_KEY in modifiers:
                        # With Que's key held the chord is Que's even when inactive
                        expected = (
                            ((2 if early_release else 3) if repeat else 1)
                            if active
                            else 0
                        )
                        ok = (
                            delivered_only_before_que(driver, order)
                            and len(driver.actions) == expected
                            and driver.clear()
                        )
                    else:
                        delivered_keys = [
                            (key, down) for key, down, _ in driver.delivered
                        ]
                        ok = (
                            not driver.actions
                            and (vk, True) in delivered_keys
                            and (vk, False) in delivered_keys
                            and driver.clear()
                        )
                        ok = ok and all(
                            (mod, True) in delivered_keys
                            and (mod, False) in delivered_keys
                            for mod in modifiers
                        )
                    check = f"hook.{name}.v{variant}.{order}.{visible}.{early_release}.{system}"
                    record(
                        check,
                        driver,
                        ok,
                        "Que's key and everything pressed after it are never delivered (modifiers pressed before it are delivered intact); inactive and non-Que chords and state recovery must remain intact.",
                    )
    for mask, vk, name, repeat, always in HOTKEYS:
        for mixed_modifiers in itertools.product(*chord_groups(mask)):
            for press_order in itertools.permutations(mixed_modifiers):
                for release_order in itertools.permutations((*mixed_modifiers, vk)):
                    for visible in (True, False):
                        driver = HookDriver(visible)
                        for mod in press_order:
                            driver.event(
                                mod,
                                flags=LLKHF_EXTENDED
                                if mod in EXTENDED_MODIFIERS
                                else 0,
                            )
                        driver.event(vk)
                        driver.event(vk)
                        action_held = True
                        for key in release_order:
                            driver.event(key, False)
                            action_held = action_held and key != vk
                            if action_held:
                                driver.event(vk)
                        active = QUE_KEY in mixed_modifiers and (always or visible)
                        downstream = [(key, down) for key, down, _ in driver.delivered]
                        held = set()
                        orphan_up = False
                        for key, down in downstream:
                            if down:
                                held.add(key)
                            elif key not in held:
                                orphan_up = True
                            else:
                                held.remove(key)
                        ok = not held and not orphan_up and driver.clear()
                        if QUE_KEY in mixed_modifiers:
                            ok = (
                                ok
                                and delivered_only_before_que(driver, press_order)
                                and driver.actions
                                == ([name] * (2 if repeat else 1) if active else [])
                            )
                        else:
                            ok = (
                                ok
                                and not driver.actions
                                and all(
                                    (key, True) in downstream
                                    and (key, False) in downstream
                                    for key in (*mixed_modifiers, vk)
                                )
                            )
                        record(
                            f"hook.permutations.{name}.{press_order}.{release_order}.{visible}",
                            driver,
                            ok,
                            "Independent mixed-modifier press/release orders, repeats after every partial release, downstream key balance and callback count.",
                        )
    driver = HookDriver()
    driver.event(QUE_KEY)
    driver.event(69)
    driver.event(QUE_KEY, False)
    driver.event(69)
    driver.event(69, False)
    record(
        "hook.ctrl-e-release-ctrl-repeat-e",
        driver,
        not driver.delivered,
        "Caps Lock+E → release Caps Lock → hold E: repeated E key-down must remain suppressed until E release.",
    )
    for seed in range(20):
        rng = random.Random(seed)
        driver = HookDriver()
        expected_delivery: list[tuple[int, bool]] = []
        expected_actions = 0
        for _ in range(40):
            ordinary = rng.choice((True, False))
            ctrl = rng.choice(CHORD_GROUPS[0][1])
            driver.event(ctrl)
            key = ord("A") if ordinary else ord("E")
            driver.event(key)
            if ctrl != QUE_KEY:
                expected_delivery += [
                    (ctrl, True),
                    (key, True),
                    (key, False),
                    (ctrl, False),
                ]
            elif not ordinary:
                expected_actions += 1
            driver.event(key, False)
            driver.event(ctrl, False)
        record(
            f"hook.generated.{seed}",
            driver,
            [(vk, down) for vk, down, _ in driver.delivered] == expected_delivery
            and len(driver.actions) == expected_actions
            and driver.clear(),
            "Seeded ordinary/hotkey sequences with Que's key and Right/Left Ctrl: independently compare complete delivered stream, callback count and released state.",
        )
    for case in (
        "visibility",
        "overlap",
        "modifier-repeat",
        "extended",
        "injected",
        "negative",
        "callback-exception",
        "interrupted",
        "shared",
        "natural-modifiers",
    ):
        driver = HookDriver()
        ok = True
        if case in ("injected", "negative"):
            for key in (0x11, 69):
                driver.event(
                    key,
                    flags=LLKHF_INJECTED if case == "injected" else 0,
                    code=-1 if case == "negative" else 0,
                )
            ok = len(driver.delivered) == 2 and not driver.actions and driver.clear()
        elif case == "callback-exception":
            driver.manager.always_active_hotkeys[(MOD_QUE, 69)] = (
                MagicMock(side_effect=RuntimeError("fixture callback failure")),
                False,
            )
            driver.event(QUE_KEY)
            try:
                driver.event(69)
            except RuntimeError:
                ok = False
        elif case == "interrupted":
            driver.event(QUE_KEY)
            driver.event(QUE_KEY, False)
            driver.event(65)
            driver.event(65, False)
            ok = (
                driver.delivered == [(65, True, False), (65, False, False)]
                and driver.clear()
            )
        elif case == "extended":
            # Right Ctrl withheld after Que's key is replayed with the next key, both extended
            driver.event(QUE_KEY)
            driver.event(0xA3, flags=LLKHF_EXTENDED)
            driver.event(QUE_KEY, False)
            driver.event(0x27, flags=LLKHF_EXTENDED)
            calls = driver.api.keybd_event.call_args_list
            ok = len(calls) == 2 and all(call.args[2] == 1 for call in calls)
        elif case == "natural-modifiers":
            # Without Que's key, modifiers reach the app at once with their auto-repeat
            presses = [
                (0xA2, True),
                (0xA2, True),
                (65, True),
                (65, False),
                (0xA2, False),
            ]
            presses += [
                (0xA0, True),
                (0xA0, True),
                (69, True),
                (69, False),
                (0xA0, False),
            ]
            for key, down in presses:
                driver.event(key, down)
            ok = (
                driver.delivered == [(key, down, False) for key, down in presses]
                and not driver.actions
                and driver.clear()
            )
        else:
            driver.event(QUE_KEY)
            if case == "visibility":
                driver.event(72)
                driver.window.isVisible.return_value = False
                driver.manager.window_visible = False
                driver.event(72)
                driver.event(72, False)
            elif case == "overlap":
                for key in (69, 72, 69):
                    driver.event(key)
                    driver.event(key, False)
            else:
                driver.event(65)
                driver.event(65, False)
                if case == "modifier-repeat":
                    driver.event(QUE_KEY)
                else:
                    driver.event(69)
                    driver.event(69, False)
            driver.event(QUE_KEY, False)
            # Que's key, its repeat and every key pressed with it stay hidden
            ok = not driver.delivered and driver.clear()
        record(
            f"hook.{case}",
            driver,
            ok,
            "Check suppression, balanced ordinary input, safe failure handling, and recovery.",
        )
    for installed in (True, False):
        driver = HookDriver()
        commands, events = MagicMock(), MagicMock()
        commands.recv_bytes.side_effect = EOFError
        host = HookHost(commands, events, library=None)  # The Python fallback path
        driver.api.SetWindowsHookExW.return_value = 42 if installed else None
        with (
            patch("core.hook_host.user32", driver.api),
            patch("core.hook_host.kernel32"),
            patch("core.hook_host.system"),
            patch("core.hook_host.native") as native_api,
            patch.object(HookHost, "_create_monitor_window", return_value=True),
            patch.object(HookHost, "_register_raw_input", return_value=True),
        ):
            native_api.GetMessageW.return_value = 0
            code = host.run()
        sent = [
            call.args[0].decode("utf-8") for call in events.send_bytes.call_args_list
        ]
        ok = (
            code == 0 and driver.api.UnhookWindowsHookEx.call_count == 2
            if installed
            else code == 1
            and not native_api.GetMessageW.called
            and any("Keyboard hook unavailable" in text for text in sent)
        )
        record(
            f"hook.installation.{installed}",
            driver,
            ok,
            "Successful installation must unhook; installation failure must be surfaced before entering a message loop.",
        )
    return results, traces
