"""Exercise the production hook decisions with real structures and mocked Win32 calls."""

import ctypes
import itertools
import random
from typing import Any
from unittest.mock import MagicMock, patch

from core.hook_engine import HookEngine
from core.hook_host import HookHost
from core.win32_hook import (
    KBDLLHOOKSTRUCT,
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
)
from stealth.manifest import HOTKEYS
from stealth.results import Result


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
    variants = [(0x11, 0x12, 0x10), (0xA2, 0xA4, 0xA0), (0xA3, 0xA5, 0xA1)]
    for mask, vk, name, repeat, always in HOTKEYS:
        for variant, keys in enumerate(variants):
            modifiers = [
                keys[index] for index, bit in enumerate((1, 2, 4)) if mask & bit
            ]
            for order in itertools.permutations(modifiers):
                for visible, early_release, system in itertools.product(
                    (True, False), (True, False), (True, False)
                ):
                    driver = HookDriver(visible)
                    for mod in order:
                        driver.event(
                            mod,
                            system=system,
                            flags=LLKHF_EXTENDED if variant == 2 else 0,
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
                    active = always or visible
                    if active:
                        expected = (2 if early_release else 3) if repeat else 1
                        ok = (
                            not driver.delivered
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
                        "Matched chords must never be delivered; inactive chords and state recovery must remain intact.",
                    )
    for mask, vk, name, repeat, always in HOTKEYS:
        choices = [
            group
            for bit, group in (
                (1, (0x11, 0xA2, 0xA3)),
                (2, (0x12, 0xA4, 0xA5)),
                (4, (0x10, 0xA0, 0xA1)),
            )
            if mask & bit
        ]
        for mixed_modifiers in itertools.product(*choices):
            for press_order in itertools.permutations(mixed_modifiers):
                for release_order in itertools.permutations((*mixed_modifiers, vk)):
                    for visible in (True, False):
                        driver = HookDriver(visible)
                        for mod in press_order:
                            driver.event(
                                mod, flags=LLKHF_EXTENDED if mod == 0xA5 else 0
                            )
                        driver.event(vk)
                        driver.event(vk)
                        action_held = True
                        for key in release_order:
                            driver.event(key, False)
                            action_held = action_held and key != vk
                            if action_held:
                                driver.event(vk)
                        active = always or visible
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
                        if active:
                            ok = (
                                ok
                                and not downstream
                                and driver.actions == [name] * (2 if repeat else 1)
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
    driver.event(0x11)
    driver.event(69)
    driver.event(0x11, False)
    driver.event(69)
    driver.event(69, False)
    record(
        "hook.ctrl-e-release-ctrl-repeat-e",
        driver,
        not driver.delivered,
        "Ctrl+E → release Ctrl → hold E: repeated E key-down must remain suppressed until E release.",
    )
    for seed in range(20):
        rng = random.Random(seed)
        driver = HookDriver()
        expected_delivery: list[tuple[int, bool]] = []
        expected_actions = 0
        for _ in range(40):
            ordinary = rng.choice((True, False))
            modifier_variant = rng.choice(variants)
            ctrl = modifier_variant[0]
            driver.event(ctrl)
            key = ord("A") if ordinary else ord("E")
            driver.event(key)
            if ordinary:
                expected_delivery += [
                    (ctrl, True),
                    (key, True),
                    (key, False),
                    (ctrl, False),
                ]
            else:
                expected_actions += 1
            driver.event(key, False)
            driver.event(ctrl, False)
        record(
            f"hook.generated.{seed}",
            driver,
            [(vk, down) for vk, down, _ in driver.delivered] == expected_delivery
            and len(driver.actions) == expected_actions
            and driver.clear(),
            "Seeded ordinary/hotkey sequences: independently compare complete delivered stream, callback count and released state.",
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
            driver.manager.always_active_hotkeys[(1, 69)] = (
                MagicMock(side_effect=RuntimeError("fixture callback failure")),
                False,
            )
            driver.event(0x11)
            try:
                driver.event(69)
            except RuntimeError:
                ok = False
        elif case == "interrupted":
            driver.event(0x11)
            driver.event(0x11, False)
            driver.event(65)
            driver.event(65, False)
            ok = (
                driver.delivered == [(65, True, False), (65, False, False)]
                and driver.clear()
            )
        elif case == "extended":
            driver.event(0xA3, flags=LLKHF_EXTENDED)
            driver.event(0x27, flags=LLKHF_EXTENDED)
            ok = all(
                call.args[2] == 1 for call in driver.api.keybd_event.call_args_list
            )
        else:
            driver.event(0x11)
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
                    driver.event(0x11)
                else:
                    driver.event(69)
                    driver.event(69, False)
            driver.event(0x11, False)
            if case in ("visibility", "overlap"):
                ok = not driver.delivered and driver.clear()
            else:
                ctrl_down = sum(vk == 0x11 and down for vk, down, _ in driver.delivered)
                ctrl_up = sum(
                    vk == 0x11 and not down for vk, down, _ in driver.delivered
                )
                ok = ctrl_down == ctrl_up == 1 and driver.clear()
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
        sent = [call.args[0].decode("utf-8") for call in events.send_bytes.call_args_list]
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
