"""Tests for the input helper's leak detector, timer, window procedure and setup paths.

The helper is where Que notices a key or click that Windows let past its hooks (so another
app saw it) and restores the hooks at once. A fake Win32 layer feeds it byte-exact Raw Input
records, so the real parsing, matching and reporting run without installing any hook.
"""

import ctypes
import json
import unittest
import winreg
from typing import Any
from unittest.mock import MagicMock, patch

from core import hook_engine, hook_host
from core.hook_engine import (
    LLMHF_INJECTED,
    TICK_MASK,
    InputMonitor,
    _MouseData,
    key_identity,
)
from core.hook_host import (
    HOOK_TIMEOUT_MS,
    RI_KEY_BREAK,
    RI_KEY_E0,
    RI_KEY_E1,
    RIM_TYPEKEYBOARD,
    RIM_TYPEMOUSE,
    TIMER_ID,
    WM_CLOSE,
    WM_HOOK_EVENTS,
    WM_INPUT,
    WM_TIMER,
    HookHost,
    _key_name,
    _raise_hook_timeout,
    _RawInputHeader,
    _RawKeyboard,
    _RawMouse,
    run_hook_host,
)
from core.native_hook import STATUS_KEYBOARD, NativeHookEngine

LEFT_DOWN, LEFT_UP, WHEEL = 0x0001, 0x0002, 0x0400
MIDDLE_DOWN, MIDDLE_UP = 0x0010, 0x0020
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEWHEEL = 0x0207, 0x0208, 0x020A


class FakeUser32:
    """Stand in for the helper's private user32 handle, serving one Raw Input record."""

    def __init__(self) -> None:
        self.record = b""
        self.short_read = False
        self.quit_posted = False
        self.raw_devices: list[tuple[int, int, int]] = []
        self.message_time = 0

    def GetRawInputData(
        self, _handle: int, _command: int, buffer: Any, size: Any, _header: int
    ) -> int:
        """Report the record's size, or copy it into the caller's buffer.

        Args:
            buffer (Any): The destination, or None to query the size.
            size (Any): A byref UINT receiving the size.

        Returns:
            int: The bytes copied.
        """
        if buffer is None:
            size._obj.value = len(self.record)
            return 0
        ctypes.memmove(buffer, self.record, len(self.record))
        return len(self.record) - 1 if self.short_read else len(self.record)

    def GetKeyNameTextW(self, _code: int, _buffer: Any, _length: int) -> int:
        """Pretend the layout has no name for the key.

        Returns:
            int: Zero characters.
        """
        return 0

    def GetMessageTime(self) -> int:
        """Return the timestamp Windows put on the current message.

        Returns:
            int: The message timestamp as a signed 32-bit value.
        """
        return self.message_time

    def PostQuitMessage(self, _code: int) -> None:
        """Record that the message loop was asked to stop."""
        self.quit_posted = True

    def DefWindowProcW(
        self, _hwnd: int, _message: int, _wparam: int, _lparam: int
    ) -> int:
        """Return the default result.

        Returns:
            int: A marker value.
        """
        return 7

    def RegisterRawInputDevices(self, devices: Any, count: int, _size: int) -> int:
        """Record the requested Raw Input registrations.

        Args:
            devices (Any): The RAWINPUTDEVICE array.
            count (int): How many entries it has.

        Returns:
            int: Success.
        """
        self.raw_devices = [
            (devices[i].usUsagePage, devices[i].usUsage, devices[i].dwFlags)
            for i in range(count)
        ]
        return 1


def keyboard_record(vkey: int, scan: int, flags: int = 0, device: int = 1) -> bytes:
    """Build a RAWINPUT keyboard record.

    Args:
        vkey (int): The virtual key.
        scan (int): The make code.
        flags (int): RI_KEY_* flags.
        device (int): The device handle; zero means injected input.

    Returns:
        bytes: The record.
    """
    header = _RawInputHeader(RIM_TYPEKEYBOARD, 0, device, 0)
    body = _RawKeyboard(scan, flags, 0, vkey, 0, 0)
    return bytes(header) + bytes(body)


def mouse_record(button_flags: int, device: int = 1) -> bytes:
    """Build a RAWINPUT mouse record.

    Args:
        button_flags (int): RI_MOUSE_* button flags.
        device (int): The device handle; zero means injected input.

    Returns:
        bytes: The record.
    """
    header = _RawInputHeader(RIM_TYPEMOUSE, 0, device, 0)
    body = _RawMouse(0, 0, button_flags, 0, 0, 0, 0, 0)
    return bytes(header) + bytes(body)


class HookHostTestCase(unittest.TestCase):
    """Build a helper on the Python fallback engine with every Win32 call faked."""

    def setUp(self) -> None:
        """Create the helper and patch the Win32 handles it uses."""
        self.user32 = FakeUser32()
        self.tick = 1000
        self.kernel32 = MagicMock()
        self.kernel32.GetTickCount.side_effect = lambda: self.tick
        self.hooks = MagicMock()
        self.hooks.SetWindowsHookExW.side_effect = [11, 12, 13, 14, 15, 16]
        for name, value in (
            ("native", self.user32),
            ("kernel32", self.kernel32),
            ("user32", self.hooks),
        ):
            patcher = patch.object(hook_host, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.commands, self.pipe = MagicMock(), MagicMock()
        self.host = HookHost(self.commands, self.pipe, library=None)
        self.assertTrue(self.host.install())

    def events(self) -> list[dict[str, Any]]:
        """Drain the events the helper queued for Que.

        Returns:
            list[dict[str, Any]]: The events, oldest first.
        """
        events = []
        while not self.host.outbox.empty():
            event = self.host.outbox.get_nowait()
            if event is not None:
                events.append(event)
        return events

    def raw(self, record: bytes) -> None:
        """Deliver one Raw Input record through the window procedure.

        Args:
            record (bytes): The RAWINPUT bytes.
        """
        self.user32.record = record
        self.assertEqual(self.host._window_proc(1, WM_INPUT, 0, 99), 7)


class RawInputLeakTests(HookHostTestCase):
    """Detect physical input that reached other apps because the hook never saw it."""

    def test_key_the_hook_missed_is_reported_and_hooks_restored(self) -> None:
        """Report a physical key only Raw Input saw, and reinstall both hooks."""
        self.raw(keyboard_record(0x41, 0x1E))
        self.tick += 1000
        self.host._window_proc(1, WM_TIMER, TIMER_ID, 0)
        leaks = [event for event in self.events() if event["type"] == "leak"]
        self.assertEqual(
            leaks, [{"type": "leak", "events": ["key 0x1E press"], "restored": True}]
        )
        self.assertEqual(
            self.hooks.SetWindowsHookExW.call_count, 4
        )  # Installed, then reinstalled
        self.assertEqual(self.host.engine.monitor.missed_events, 1)  # type: ignore[union-attr]

    def test_key_the_hook_saw_is_not_a_leak(self) -> None:
        """Match Raw Input against the hook's own observation of the same key."""
        monitor = self.host.engine.monitor
        assert monitor is not None
        monitor.record_hook(key_identity(0x41, 0x1E, False, True), self.tick)
        self.raw(keyboard_record(0x41, 0x1E))
        self.tick += 1000
        self.host._on_timer()
        self.assertFalse([event for event in self.events() if event["type"] == "leak"])

    def test_release_and_extended_keys_are_named(self) -> None:
        """Describe a missed release of an extended key."""
        self.raw(keyboard_record(0x25, 0x4B, RI_KEY_BREAK | RI_KEY_E0))
        self.tick += 1000
        self.host._on_timer()
        self.assertEqual(self.events()[-1]["events"], ["key 0x4B release"])

    def test_injected_prefix_and_short_records_are_ignored(self) -> None:
        """Ignore Que's own replays, prefix bytes, fake shifts and truncated reads."""
        monitor = self.host.engine.monitor
        assert monitor is not None
        self.raw(keyboard_record(0x41, 0x1E, device=0))
        self.raw(keyboard_record(0xFF, 0x2A))
        self.raw(keyboard_record(0x13, 0x1D, RI_KEY_E1))
        self.user32.short_read = True
        self.raw(keyboard_record(0x41, 0x1E))
        self.assertEqual(monitor.raw_events, 0)

    def test_mouse_buttons_and_wheel_are_tracked(self) -> None:
        """Report missed clicks and wheel turns by name."""
        self.raw(mouse_record(LEFT_DOWN | WHEEL))
        self.raw(mouse_record(LEFT_UP))
        self.tick += 1000
        self.host._on_timer()
        self.assertEqual(
            self.events()[-1]["events"],
            ["left mouse button press", "mouse wheel", "left mouse button release"],
        )

    def test_trackpad_click_is_routed_without_a_false_leak(self) -> None:
        """Consume a touchpad middle click and match Raw Input in either order."""
        for engine in (self.host.fallback, NativeHookEngine(self.host.outbox.put)):
            if isinstance(engine, NativeHookEngine):
                self.addCleanup(engine.close)
            with self.subTest(engine=type(engine).__name__):
                self.host.engine = engine
                engine.monitor = InputMonitor()
                engine.apply({"type": "routing", "value": True})
                engine.apply({"type": "regions", "value": [(1, 0, 0, 100, 100)]})
                self.raw(mouse_record(0))  # Movement alone is not tracked
                self.raw(mouse_record(LEFT_DOWN, device=0))  # No device to compare
                for message, raw_flag, raw_first, consumed in (
                    (WM_MBUTTONDOWN, MIDDLE_DOWN, True, 1),
                    (WM_MBUTTONUP, MIDDLE_UP, False, 1),
                    (WM_MOUSEWHEEL, WHEEL, True, 0),
                ):
                    self.user32.message_time = -0x7FFFFFFF + message
                    if raw_first:
                        self.raw(mouse_record(raw_flag))
                    self.assertEqual(self.injected_mouse(message), consumed)
                    if not raw_first:
                        self.raw(mouse_record(raw_flag))
                self.tick += 1000
                self.host._on_timer()
                self.assertEqual(engine.monitor.raw_events, 3)
                self.assertEqual(engine.monitor.missed_events, 0)
                self.assertEqual(
                    [
                        (event["type"], event.get("message"), event.get("hwnd"))
                        for event in self.events()
                    ],
                    [("pointer", WM_MBUTTONDOWN, 1), ("pointer", WM_MBUTTONUP, 1)],
                )
                self.assertEqual(self.hooks.SetWindowsHookExW.call_count, 2)

    def test_unrelated_injected_click_does_not_mask_missed_click(self) -> None:
        """Report a missed click even after an injected callback for the same button."""
        self.user32.message_time = 1234
        self.assertEqual(self.injected_mouse(WM_MBUTTONDOWN), 0)
        self.user32.message_time += 16
        self.raw(mouse_record(MIDDLE_DOWN))
        self.tick += 1000
        self.host._on_timer()
        self.assertEqual(
            self.events(),
            [
                {
                    "type": "leak",
                    "events": ["middle mouse button press"],
                    "restored": True,
                }
            ],
        )

    def test_without_raw_input_nothing_is_recorded(self) -> None:
        """Skip Raw Input when leak detection is unavailable."""
        self.host.engine.monitor = None
        self.raw(keyboard_record(0x41, 0x1E))
        self.host._on_timer()
        self.assertFalse(self.events())

    def injected_mouse(self, message: int) -> int:
        """Deliver an injected mouse callback.

        Args:
            message (int): The mouse button or wheel message.

        Returns:
            int: One if the hook consumes the event, zero if it passes through.
        """
        tick = self.user32.message_time & TICK_MASK
        engine = self.host.engine
        if isinstance(engine, NativeHookEngine):
            return engine.process_pointer(0, message, 10, 10, 0, LLMHF_INJECTED, tick)
        event = _MouseData(flags=LLMHF_INJECTED, time=tick)
        event.pt.x = event.pt.y = 10
        self.hooks.CallNextHookEx.return_value = 0
        with patch.object(hook_engine, "user32", self.hooks):
            return engine._mouse_proc(0, message, ctypes.addressof(event))


class TimerAndWindowTests(HookHostTestCase):
    """Run the periodic heartbeat, shutdown and message routing."""

    def test_heartbeat_every_tenth_tick(self) -> None:
        """Send counts to Que once a second."""
        for _ in range(10):
            self.host._on_timer()
        heartbeats = [event for event in self.events() if event["type"] == "heartbeat"]
        self.assertEqual(
            heartbeats, [{"type": "heartbeat", "raw_events": 0, "missed_events": 0}]
        )

    def test_native_engine_heartbeat_and_drain(self) -> None:
        """Drain the compiled engine and report its timing health."""
        engine = MagicMock()
        engine.stats.return_value = {"late_events": 2, "max_late_ms": 5, "status": 0}
        self.host.native = engine
        self.host.ticks = 9
        self.host._window_proc(1, WM_HOOK_EVENTS, 0, 0)
        self.host._on_timer()
        self.assertEqual(engine.drain.call_count, 2)
        heartbeat = self.events()[-1]
        self.assertEqual(
            (heartbeat["native"], heartbeat["late_events"], heartbeat["max_late_ms"]),
            (True, 2, 5),
        )

    def test_quit_once_disconnected_and_drained(self) -> None:
        """Stop only after Que disconnects and no owned press remains."""
        self.host._on_timer()
        self.assertFalse(self.user32.quit_posted)
        self.host.connected = False
        self.host._on_timer()
        self.assertTrue(self.user32.quit_posted)

    def test_close_and_errors_never_unwind_into_windows(self) -> None:
        """Close on WM_CLOSE, and turn handler errors into events."""
        self.assertEqual(self.host._window_proc(1, WM_CLOSE, 0, 0), 0)
        self.assertTrue(self.user32.quit_posted)
        with patch.object(self.host, "_on_timer", side_effect=RuntimeError("boom")):
            self.assertEqual(self.host._window_proc(1, WM_TIMER, TIMER_ID, 0), 7)
        self.assertEqual(
            self.events()[-1], {"type": "error", "text": "Input monitor error: boom"}
        )

    def test_raw_input_listens_to_keyboards_and_mice_in_the_background(self) -> None:
        """Register both device classes with RIDEV_INPUTSINK."""
        self.assertTrue(self.host._register_raw_input())
        self.assertEqual(self.user32.raw_devices, [(1, 6, 0x100), (1, 2, 0x100)])


class InstallTests(HookHostTestCase):
    """Report every hook installation failure, since it means keys are not hidden."""

    def test_fallback_hook_failures_are_critical(self) -> None:
        """Report a missing keyboard or mouse hook and leave nothing half-installed."""
        self.host.uninstall()
        self.hooks.SetWindowsHookExW.side_effect = [0]
        self.assertFalse(self.host.install())
        self.hooks.SetWindowsHookExW.side_effect = [21, 0]
        self.assertFalse(self.host.install())
        self.hooks.UnhookWindowsHookEx.assert_called_with(21)
        errors = [event for event in self.events() if event["type"] == "error"]
        self.assertEqual(
            [event["text"].split(" ")[0] for event in errors], ["Keyboard", "Mouse"]
        )
        self.assertTrue(all(event["critical"] for event in errors))

    def test_native_engine_start_reinstall_and_failures(self) -> None:
        """Start or reinstall the compiled engine and name the failing hook."""
        engine = MagicMock()
        engine.started = False
        engine.start.return_value = STATUS_KEYBOARD
        self.host.native = engine
        self.assertTrue(self.host.install())
        engine.started = True
        engine.reinstall.return_value = 0
        for stats, text in (
            (
                {"mouse_error": 5, "keyboard_error": 0},
                "Mouse hook unavailable (Windows error 5).",
            ),
            (
                {"mouse_error": 0, "keyboard_error": 6},
                "Keyboard hook unavailable (Windows error 6).",
            ),
        ):
            engine.stats.return_value = stats
            self.assertFalse(self.host.install())
            self.assertEqual(
                self.events()[-1], {"type": "error", "text": text, "critical": True}
            )
        self.host.uninstall()
        engine.stop.assert_called_once()


class LifecycleTests(HookHostTestCase):
    """Start, run and stop the helper, failing closed when its window or hooks are missing."""

    def setUp(self) -> None:
        """Add the window and message-loop calls to the fake user32."""
        super().setUp()
        self.window_calls = MagicMock()
        self.window_calls.RegisterClassExW.return_value = 1
        self.window_calls.CreateWindowExW.return_value = 77
        self.window_calls.SetTimer.return_value = 1
        self.window_calls.GetMessageW.return_value = 0  # The loop ends at once
        for name in ("RegisterClassExW", "CreateWindowExW", "SetTimer", "GetMessageW"):
            setattr(self.user32, name, getattr(self.window_calls, name))
        system = MagicMock()
        system.GetModuleHandleW.return_value = 0
        system.GetCurrentThreadId.return_value = 1
        patcher = patch.object(hook_host, "system", system)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hooks.GetKeyState.return_value = 0  # Caps Lock already off
        self.commands.recv_bytes.side_effect = EOFError
        self.host.uninstall()
        self.hooks.reset_mock()
        self.hooks.GetKeyState.return_value = 0
        self.hooks.SetWindowsHookExW.side_effect = [31, 32]

    def test_monitor_window_creation_and_failures(self) -> None:
        """Create the message-only window, and report a window or timer failure as critical."""
        self.assertTrue(self.host._create_monitor_window())
        self.assertEqual(self.host.window, 77)
        self.window_calls.SetTimer.return_value = 0
        self.assertFalse(self.host._create_monitor_window())
        self.assertTrue(self.events()[-1]["critical"])
        self.window_calls.RegisterClassExW.return_value = 0
        self.assertFalse(self.host._create_monitor_window())

    def test_run_without_window_installs_no_hooks(self) -> None:
        """Exit with status 1 before installing hooks when the monitor window fails."""
        self.window_calls.RegisterClassExW.return_value = 0
        self.assertEqual(self.host.run(), 1)
        self.hooks.SetWindowsHookExW.assert_not_called()

    def test_run_warns_when_raw_input_is_unavailable(self) -> None:
        """Keep hiding input without leak detection, but tell Que it is unprotected."""
        self.user32.RegisterRawInputDevices = MagicMock(return_value=0)  # type: ignore[method-assign]
        self.assertEqual(self.host.run(), 0)
        self.assertIsNone(self.host.engine.monitor)
        sent = [
            json.loads(call.args[0]) for call in self.pipe.send_bytes.call_args_list
        ]  # The writer thread drained the queue
        texts = [event.get("text", "") for event in sent]
        self.assertTrue(any("Raw Input is unavailable" in text for text in texts))
        self.hooks.UnhookWindowsHookEx.assert_any_call(31)  # Hooks removed on shutdown

    def test_lost_pipe_stops_the_writer_and_releases_state(self) -> None:
        """Stop forwarding when Que's pipe breaks, and claim no new input once Que is gone."""
        self.pipe.send_bytes.side_effect = OSError
        self.host._send({"type": "ready"})
        self.host._write_events()  # Returns instead of raising
        self.host._read_commands()
        self.assertFalse(self.host.connected)


class SetupTests(unittest.TestCase):
    """Check process setup that runs before any input is handled."""

    def test_unreachable_que_exits_without_hooks(self) -> None:
        """Exit with status 2 when Que's pipe cannot be reached."""
        with (
            patch.object(hook_host, "Client", side_effect=OSError),
            patch.object(hook_host, "HookHost") as host,
        ):
            self.assertEqual(run_hook_host(r"\\.\pipe\missing", "00"), 2)
        host.assert_not_called()

    def test_hook_timeout_is_raised_only_when_lower(self) -> None:
        """Give the hooks Windows' longest deadline without lowering a higher one."""
        for current, written in ((5000, False), (200, True), (OSError, True)):
            with (
                self.subTest(current=current),
                patch.object(hook_host, "winreg") as registry,
            ):
                (
                    registry.HKEY_CURRENT_USER,
                    registry.KEY_READ,
                    registry.KEY_SET_VALUE,
                ) = 0, 1, 2
                registry.REG_DWORD = winreg.REG_DWORD
                if current is OSError:
                    registry.QueryValueEx.side_effect = OSError
                else:
                    registry.QueryValueEx.return_value = (current, winreg.REG_DWORD)
                _raise_hook_timeout()
                self.assertEqual(registry.SetValueEx.called, written)
                if written:
                    self.assertEqual(
                        registry.SetValueEx.call_args.args[-1], HOOK_TIMEOUT_MS
                    )
        with patch.object(hook_host, "winreg") as registry:
            registry.OpenKey.side_effect = OSError
            _raise_hook_timeout()  # Only margin: a locked registry must not stop the helper

    def test_key_names_come_from_the_layout(self) -> None:
        """Name keys through the real layout, with a fallback for unnamed codes."""
        self.assertEqual(_key_name(0x1E, False), "A")
        with patch.object(hook_host.native, "GetKeyNameTextW", return_value=0):
            self.assertEqual(_key_name(0x7F, True), "key 0x7F")


if __name__ == "__main__":
    unittest.main()
