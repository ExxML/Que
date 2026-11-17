"""Check input ownership, native surfaces, and source-health repair contracts."""

import ctypes
import itertools
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from PyQt6.QtWidgets import QApplication
from stealth.hooks import HookDriver

from core.hook_client import HookClient
from core.hook_engine import HookEngine, InputMonitor, _MouseData, key_identity
from core.hook_host import HookHost
from core.settings import SettingsStore
from core.transcriber import Transcriber
from ui.input.input_router import KeyTranslator, native
from ui.window.main_window import MainWindow


class OwnershipTests(unittest.TestCase):
    """Keep releases paired across shutdown, UI transitions, and failed actions."""

    def test_quit_waits_for_every_chord_release_order(self) -> None:
        """Require zero downstream events and one quit after the last physical release."""
        for order in itertools.permutations((0xA2, 0xA4, 0xA0, ord("Q"))):
            with self.subTest(order=order):
                driver = HookDriver()
                driver.manager.always_active_hotkeys[(7, ord("Q"))] = (driver.manager.request_quit, False)
                for vk in (0xA2, 0xA4, 0xA0, ord("Q")):
                    driver.event(vk)
                self.assertEqual(self._quits(driver), 0)
                for index, vk in enumerate(order):
                    driver.event(vk, False)
                    if ord("Q") in driver.manager.suppressed_vk_codes:
                        driver.event(ord("Q"))
                    self.assertEqual(self._quits(driver), int(index == len(order) - 1))
                self.assertEqual(driver.delivered, [])
                self.assertTrue(driver.clear())

    def test_owned_input_drains_after_disarm(self) -> None:
        """Consume repeats and releases even when input is disarmed while a key is held."""
        driver = HookDriver()
        driver.manager.input_armed = True
        driver.event(ord("A"))
        driver.manager.input_armed = False
        driver.event(ord("A"))
        driver.event(ord("A"), False)
        self.assertFalse(driver.delivered)
        self.assertEqual(len([event for event in driver.events if event["type"] == "key"]), 3)
        self.assertTrue(driver.clear())
        driver.event(ord("B"))
        driver.event(ord("B"), False)
        self.assertEqual([(vk, down) for vk, down, _ in driver.delivered], [(ord("B"), True), (ord("B"), False)])

    def test_already_delivered_key_cannot_be_claimed_mid_press(self) -> None:
        """Preserve the recipient's release if a shortcut or arming begins during a hold."""
        driver = HookDriver()
        driver.event(ord("E"))
        driver.event(0x11)
        driver.manager.input_armed = True
        driver.event(ord("E"))
        driver.event(ord("E"), False)
        driver.event(0x11, False)
        self.assertFalse(driver.actions)
        self.assertIn((ord("E"), False, False), driver.delivered)
        self.assertTrue(driver.clear())

    def test_callback_baseexception_is_reported_and_release_is_owned(self) -> None:
        """Contain even SystemExit from callbacks and retain subsequent hook operation."""
        driver = HookDriver()
        driver.manager.always_active_hotkeys[(1, ord("E"))] = (MagicMock(side_effect=SystemExit("fixture")), False)
        driver.event(0x11)
        driver.event(ord("E"))
        driver.event(0x11, False)
        driver.event(ord("E"), False)
        self.assertEqual(len([event for event in driver.events if event["type"] == "error"]), 1)
        self.assertFalse(driver.delivered)
        self.assertTrue(driver.clear())

    def test_windows_modifier_does_not_trigger_a_shorter_que_chord(self) -> None:
        """Leave Win+Ctrl+E intact instead of matching the Ctrl+E subset."""
        driver = HookDriver()
        for vk in (0x5B, 0x11, ord("E")):
            driver.event(vk)
        for vk in (ord("E"), 0x11, 0x5B):
            driver.event(vk, False)
        self.assertFalse(driver.actions)
        self.assertTrue(driver.clear())
        self.assertEqual([(vk, down) for vk, down, _ in driver.delivered], [(0x5B, True), (0x11, True), (ord("E"), True), (ord("E"), False), (0x11, False), (0x5B, False)])

    def test_failed_install_reports_and_never_enters_message_loop(self) -> None:
        """Verify the reported error and early return before the helper's message loop."""
        commands, events = MagicMock(), MagicMock()
        commands.recv_bytes.side_effect = EOFError
        host = HookHost(commands, events)
        with patch("core.hook_host.user32") as api, patch("core.hook_host.kernel32") as kernel, patch("core.hook_host.system"), patch("core.hook_host.native") as native_api, patch.object(HookHost, "_create_monitor_window", return_value=True):
            api.SetWindowsHookExW.return_value = None
            kernel.GetLastError.return_value = 5
            self.assertEqual(host.run(), 1)
        self.assertFalse(native_api.GetMessageW.called)
        sent = [json.loads(call.args[0]) for call in events.send_bytes.call_args_list]
        self.assertIn("error 5", sent[0]["text"])

    def test_mouse_press_is_owned_across_exit_and_routing_toggle(self) -> None:
        """Suppress releases outside Que while motion continues downstream."""
        pointer = HookEngine(MagicMock())
        pointer.regions = ((42, 10, 10, 100, 100),)
        with patch("core.hook_engine.user32") as api:
            api.CallNextHookEx.return_value = 0
            data = _MouseData()
            data.pt.x = data.pt.y = 20
            self.assertEqual(pointer._mouse_proc(0, 0x201, ctypes.addressof(data)), 1)
            pointer.routing_enabled = False
            data.pt.x = data.pt.y = 200
            self.assertEqual(pointer._mouse_proc(0, 0x200, ctypes.addressof(data)), 0)
            self.assertEqual(pointer._mouse_proc(0, 0x202, ctypes.addressof(data)), 1)
            self.assertFalse(pointer.consumed_buttons)
            self.assertEqual(pointer._mouse_proc(0, 0x201, ctypes.addressof(data)), 0)
            pointer.routing_enabled = True
            data.pt.x = data.pt.y = 20
            self.assertEqual(pointer._mouse_proc(0, 0x202, ctypes.addressof(data)), 0)

    def test_stale_arming_cannot_override_a_newer_press(self) -> None:
        """Ignore Que's answer to an older press once the helper has seen a newer one."""
        engine = HookEngine(MagicMock())
        engine.press_seq = 5
        engine.apply({"type": "armed", "value": True, "seq": 4})
        self.assertFalse(engine.input_armed)
        engine.apply({"type": "armed", "value": True, "seq": 5})
        self.assertTrue(engine.input_armed)

    def test_failed_topmost_observation_is_unknown_and_can_recover(self) -> None:
        """Keep a failed geometry observation distinct from confirmed visibility."""
        window = MagicMock()
        window.frameGeometry.side_effect = RuntimeError("fixture")
        self.assertIsNone(MainWindow._is_topmost_window(window))
        window.chat_area.show_stream_error.assert_called_once()


    @staticmethod
    def _quits(driver: HookDriver) -> int:
        """Count the quit confirmations the engine sent to Que."""
        return len([event for event in driver.events if event["type"] == "quit_ready"])


class TypingModeTests(unittest.TestCase):
    """While typing in Que, swallow what the user expects swallowed and pass what they expect to work."""

    def armed(self) -> HookDriver:
        """Create a driver whose input is in typing mode."""
        driver = HookDriver()
        driver.manager.input_armed = True
        return driver

    def press(self, driver: HookDriver, *keys: int) -> None:
        """Press keys in order and release them in reverse order."""
        for key in keys:
            driver.event(key)
        for key in reversed(keys):
            driver.event(key, False)

    def test_ordinary_typing_and_editing_keys_are_swallowed(self) -> None:
        """Keep characters, editing keys and ordinary chords inside Que."""
        for keys in ((ord("A"),), (0x0D,), (0x09,), (0x1B,), (0x08,), (0x25,), (0x24,), (0x21,), (0xA2, ord("W")), (0xA2, ord("V")), (0xA0, ord("A")), (0xA4, ord("D"))):
            with self.subTest(keys=keys):
                driver = self.armed()
                self.press(driver, *keys)
                self.assertFalse(driver.delivered)
                self.assertTrue(driver.manager.input_armed)
                self.assertTrue(driver.clear())

    def test_navigation_chords_reach_windows_and_leave_typing_mode(self) -> None:
        """Deliver Alt+Tab, Alt+Esc, Ctrl+Esc, Ctrl+Shift+Esc and Alt+F4 intact, then stop typing."""
        for keys in ((0xA4, 0x09), (0xA4, 0xA0, 0x09), (0xA4, 0x1B), (0xA2, 0x1B), (0xA2, 0xA0, 0x1B), (0xA4, 0x73)):
            with self.subTest(keys=keys):
                driver = self.armed()
                self.press(driver, *keys)
                delivered = [(vk, down) for vk, down, _ in driver.delivered]
                self.assertEqual(sorted(delivered), sorted([(vk, True) for vk in keys] + [(vk, False) for vk in keys]))
                self.assertFalse(driver.manager.input_armed)
                self.assertIn({"type": "armed", "value": False, "seq": 0}, driver.events)
                self.assertTrue(driver.clear())

    def test_alt_tab_cycles_while_alt_stays_held(self) -> None:
        """Pass every Tab press of one held-Alt switch and the final Alt release."""
        driver = self.armed()
        driver.event(0xA4)
        for _ in range(3):
            driver.event(0x09)
            driver.event(0x09, False)
        driver.event(0xA4, False)
        self.assertEqual([(vk, down) for vk, down, _ in driver.delivered], [(0xA4, True)] + [(0x09, True), (0x09, False)] * 3 + [(0xA4, False)])
        self.assertTrue(driver.clear())

    def test_windows_key_combinations_reach_windows(self) -> None:
        """Pass the Windows key alone and with other keys, then stop typing."""
        for keys in ((0x5B,), (0x5B, ord("D")), (0x5C, ord("E")), (0x5B, 0xA0, ord("S"))):
            with self.subTest(keys=keys):
                driver = self.armed()
                self.press(driver, *keys)
                self.assertEqual(len(driver.delivered), 2 * len(keys))
                self.assertFalse(driver.manager.input_armed)
                self.assertTrue(driver.clear())

    def test_function_device_and_lock_keys_keep_working_while_typing(self) -> None:
        """Pass F1-F12, Print Screen, media, volume and lock keys without leaving typing mode."""
        for keys in ((0x74,), (0x7A,), (0xA2, 0x74), (0x2C,), (0xAF,), (0xAD,), (0xB3,), (0x14,), (0x90,)):
            with self.subTest(keys=keys):
                driver = self.armed()
                self.press(driver, *keys)
                self.assertEqual(len(driver.delivered), 2 * len(keys))
                self.assertTrue(driver.manager.input_armed)
                self.assertTrue(driver.clear())

    def test_lock_keys_are_reported_to_que_in_and_out_of_typing_mode(self) -> None:
        """Let Que track Caps Lock pressed anywhere, since it never has native focus."""
        for armed in (True, False):
            with self.subTest(armed=armed):
                driver = HookDriver()
                driver.manager.input_armed = armed
                self.press(driver, 0x14)
                driver.event(0x14)
                driver.event(0x14)  # Autorepeat must not toggle again
                driver.event(0x14, False)
                self.assertEqual([event for event in driver.events if event["type"] == "lock"], [{"type": "lock", "vk": 0x14}] * 2)
                self.assertFalse([event for event in driver.events if event["type"] == "key"])
                self.assertEqual(len(driver.delivered), 5)

    def test_que_shortcuts_still_win_while_typing(self) -> None:
        """Run Que's own chords instead of typing them."""
        driver = self.armed()
        self.press(driver, 0xA2, ord("E"))
        self.assertEqual(driver.actions, ["toggle_window_visibility"])
        self.assertFalse(driver.delivered)


class InputMonitorTests(unittest.TestCase):
    """Detect physical input that Raw Input saw but the hooks did not."""

    def test_events_seen_by_both_in_either_order_are_not_missed(self) -> None:
        """Match hook-first and Raw-Input-first arrivals."""
        monitor = InputMonitor()
        monitor.record_hook(key_identity(0x41, 0x1E, False, True), 100)
        monitor.record_raw(key_identity(0x41, 0x1E, False, True), 101, "A press")
        monitor.record_raw(key_identity(0x41, 0x1E, False, False), 102, "A release")
        monitor.record_hook(key_identity(0x41, 0x1E, False, False), 103)
        self.assertEqual(monitor.expire(10_000), [])

    def test_raw_only_event_is_reported_after_the_grace_period(self) -> None:
        """Report a key the hook never saw, but only once the hook had time to see it."""
        monitor = InputMonitor(grace_ms=300)
        monitor.record_raw(key_identity(0x45, 0x12, False, True), 1000, "E press")
        self.assertEqual(monitor.expire(1200), [])
        self.assertEqual(monitor.expire(1400), ["E press"])
        self.assertEqual(monitor.expire(1500), [])

    def test_side_specific_and_scanless_keys_match(self) -> None:
        """Match Left Ctrl to Raw Input's generic Ctrl, and a media key without a scan code."""
        monitor = InputMonitor()
        monitor.record_hook(key_identity(0xA2, 0x1D, False, True), 0)
        monitor.record_raw(key_identity(0x11, 0x1D, False, True), 1, "Ctrl press")
        monitor.record_hook(key_identity(0xAF, 0x30, True, True), 2)
        monitor.record_raw(key_identity(0xAF, 0, False, True), 3, "Volume up press")
        self.assertEqual(monitor.expire(10_000), [])

    def test_tick_count_wraparound(self) -> None:
        """Measure ages across the 49.7-day tick-count wrap."""
        monitor = InputMonitor(grace_ms=300)
        monitor.record_raw(frozenset({("button", 1, True)}), 0xFFFFFFF0, "left mouse button press")
        self.assertEqual(monitor.expire(0x100), [])
        self.assertEqual(monitor.expire(0x200), ["left mouse button press"])


class HelperWatchdogTests(unittest.TestCase):
    """Restart a silent helper without false alarms when Que itself was paused."""

    def setUp(self) -> None:
        """Create a client that believes it is connected, without starting a process."""
        self.app = QApplication.instance() or QApplication([])
        self.client = HookClient()
        self.client.connected = True
        self.alerts: list[str] = []
        self.client.alert.connect(self.alerts.append)

    def test_silent_helper_is_restarted_with_a_warning(self) -> None:
        """Warn and restart when the pipe thread has heard nothing for longer than the limit."""
        now = time.monotonic()
        self.client.last_check = now - 1
        self.client.last_received = now - 10
        with patch.object(self.client, "_spawn") as spawn:
            self.client._check_helper()
        spawn.assert_called_once()
        self.assertEqual(len(self.alerts), 1)

    def test_paused_que_does_not_raise_a_false_alarm(self) -> None:
        """Skip the check after Que was suspended or blocked, then detect real silence later."""
        now = time.monotonic()
        self.client.last_check = now - 30
        self.client.last_received = now - 30
        with patch.object(self.client, "_spawn") as spawn:
            self.client._check_helper()
        spawn.assert_not_called()
        self.assertEqual(self.alerts, [])


class HelperProcessTests(unittest.TestCase):
    """Start the real helper process, which briefly installs real low-level hooks."""

    def test_helper_connects_answers_quit_and_exits(self) -> None:
        """Connect over the authenticated pipe, confirm a quit, and exit once disconnected."""
        app = QApplication.instance() or QApplication([])
        client = HookClient()
        failures: list[str] = []
        quits: list[bool] = []
        client.quit_ready.connect(lambda: quits.append(True))
        client.failed.connect(failures.append)
        client.alert.connect(failures.append)
        client.set_visible(False)
        client.start()
        deadline = time.monotonic() + 30
        while not client.connected and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        self.assertTrue(client.connected, failures)
        self.assertTrue(client.request_quit())
        deadline = time.monotonic() + 5
        while not quits and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        self.assertEqual(quits, [True])
        process = client.process
        client.stop()
        assert process is not None
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(failures, [])


class AudioHealthTests(unittest.TestCase):
    """Distinguish a silent or dead source without pretending to know its physical cause."""

    def setUp(self) -> None:
        """Create a signal-capable application and an isolated settings store."""
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.transcriber = Transcriber(SettingsStore(Path(self.directory.name) / "settings.json"))

    def test_portaudio_host_and_streams_share_one_owner_thread(self) -> None:
        """Initialize, open and close both sources on the same thread with one host."""
        stop = threading.Event()
        threads = []
        audio = MagicMock()
        audio.get_default_wasapi_loopback.return_value = {"maxInputChannels": 1, "defaultSampleRate": 16000, "index": 1}
        audio.get_device_info_by_index.return_value = {"maxInputChannels": 1, "defaultSampleRate": 16000, "index": 2}
        def open_stream(**_kwargs):
            """Record the owner thread and stop after opening the second stream."""
            threads.append(threading.get_ident())
            if len(threads) == 2:
                stop.set()
            return MagicMock()
        audio.open.side_effect = open_stream
        with patch("core.transcriber.pyaudiowpatch.PyAudio", return_value=audio) as create:
            items = queue.Queue()
            self.transcriber._capture_sources(("you", "them"), stop, items)
        create.assert_called_once()
        self.assertEqual(threads, [threading.get_ident()] * 2)
        audio.terminate.assert_called_once()
        self.assertEqual([items.get_nowait(), items.get_nowait()], [None, None])

    def test_only_a_microphone_without_data_is_reported(self) -> None:
        """Warn when the microphone stops delivering audio, never for silence or a quiet loopback."""
        for source, silence in itertools.product(("you", "them"), (None, np.zeros(1600, np.float32).tobytes())):
            with self.subTest(source=source, callbacks=silence is not None):
                transcriber = self.transcriber
                stop = threading.Event()
                statuses: list[tuple[object, ...]] = []
                transcriber.source_status.connect(lambda *args, statuses=statuses: statuses.append(args))
                utterances: queue.Queue[object] = queue.Queue()
                stream = MagicMock()
                stream.is_active.return_value = True
                audio = MagicMock()
                audio.open.return_value = stream
                device = {"maxInputChannels": 1, "defaultSampleRate": 16000, "index": 1}
                audio.get_default_wasapi_loopback.return_value = device
                audio.get_host_api_info_by_type.return_value = {"defaultInputDevice": 1}
                audio.get_device_info_by_index.return_value = device
                count = 0

                def receive(*_args: object, stop: threading.Event = stop, silence: bytes | None = silence, **_kwargs: object) -> bytes:
                    """Return synthetic source data and end after observing recovery."""
                    nonlocal count
                    count += 1
                    if count >= 3:
                        stop.set()
                        return np.full(1600, 0.1, np.float32).tobytes()
                    if silence is None:
                        raise queue.Empty
                    return silence

                with patch("core.transcriber.pyaudiowpatch.PyAudio", return_value=audio), patch("core.transcriber.time.monotonic", side_effect=(0, 6, 7, 8)), patch("core.transcriber.queue.Queue") as queue_type:
                    queue_type.return_value.get.side_effect = receive
                    transcriber._capture(source, stop, utterances)  # type: ignore[arg-type]
                messages = [row[1] for row in statuses]
                if source == "you" and silence is None:
                    self.assertIn("no audio data", messages[0])
                    self.assertEqual(messages[-1], "")
                else:
                    self.assertEqual(messages, [])
                items = []
                while not utterances.empty():
                    items.append(utterances.get_nowait())
                self.assertIsNone(items[-1])
                self.assertEqual(sum(item is None for item in items), 1)

    def test_silent_source_never_paces_the_other_source(self) -> None:
        """Drain a backlog of microphone audio promptly while the loopback delivers nothing."""
        from core.transcriber import _CaptureState

        captures = {}
        for source in ("you", "them"):
            capture = _CaptureState(source, 1, 16000)
            capture.stream = MagicMock()
            capture.stream.is_active.return_value = True
            captures[source] = capture
        for _ in range(30):
            captures["you"].chunks.put(np.zeros(1600, np.float32).tobytes())
        stop = threading.Event()
        utterances: queue.Queue[tuple[str, object] | None] = queue.Queue()
        original_poll = self.transcriber._poll_source

        def poll(capture: object, stop_event: threading.Event, timeout: float) -> object:
            """Stop once the microphone backlog is gone."""
            result = original_poll(capture, stop_event, timeout)  # type: ignore[arg-type]
            if captures["you"].chunks.empty():
                stop.set()
            return result

        started = time.monotonic()
        with patch("core.transcriber.pyaudiowpatch.PyAudio"), patch.object(self.transcriber, "_open_source", side_effect=lambda _audio, source: captures[source]), patch.object(self.transcriber, "_poll_source", side_effect=poll):
            self.transcriber._capture_sources(("you", "them"), stop, utterances)  # type: ignore[arg-type]
        # Pacing by the silent source would take 30 x 0.1 s; draining must take far less
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(captures["you"].chunks.empty())

    def test_dead_stream_and_stale_session(self) -> None:
        """Report dead streams and keep old-session health out of a new recording."""
        transcriber = self.transcriber
        transcriber.recording = True
        transcriber._on_source_status("them", "old error", threading.Event())
        self.assertFalse(transcriber.source_health)
        transcriber._on_source_status("them", "No data", transcriber.stop_event)
        self.assertEqual(transcriber.source_health["them"], "No data")
        transcriber._on_source_status("them", "", transcriber.stop_event)
        self.assertEqual(transcriber.source_health["them"], "")

    def test_inactive_stream_reports_and_finishes(self) -> None:
        """Surface an audio device that stopped after opening instead of feeding silence forever."""
        audio = MagicMock()
        audio.get_default_wasapi_loopback.return_value = {"maxInputChannels": 1, "defaultSampleRate": 16000, "index": 1}
        audio.open.return_value.is_active.return_value = False
        failed = MagicMock()
        self.transcriber.failed.connect(failed)
        utterances = queue.Queue()
        with patch("core.transcriber.pyaudiowpatch.PyAudio", return_value=audio):
            self.transcriber._capture("them", threading.Event(), utterances)
        self.assertIn("stopped delivering", failed.call_args.args[0])
        self.assertIsNone(utterances.get_nowait())
        audio.terminate.assert_called_once()

    def test_unrecognized_sound_is_not_reported_as_a_failure(self) -> None:
        """Skip non-speech sound quietly and keep transcribing the speech that follows."""
        model = MagicMock()
        model.transcribe.side_effect = [([], None), ([MagicMock(text="fixture")], None)]
        queue_items: queue.Queue[object] = queue.Queue()
        for _ in range(2):
            queue_items.put(("them", np.ones(16000, np.float32)))
        queue_items.put(None)
        queue_items.put(None)
        health: list[tuple[object, ...]] = []
        lines: list[tuple[str, str]] = []
        self.transcriber.source_status.connect(lambda *args: health.append(args))
        self.transcriber.transcribed.connect(lambda source, text: lines.append((source, text)))
        with patch.object(self.transcriber, "_load_model", return_value=model):
            self.transcriber._transcribe_loop(queue_items, "tiny.en")  # type: ignore[arg-type]
        self.assertEqual(health, [])
        self.assertEqual(lines, [("them", "fixture")])


class NativeSurfaceTests(unittest.TestCase):
    """Run a real Windows surface probe separately from offscreen regression widgets."""

    def test_every_visible_native_surface_is_excluded(self) -> None:
        """Include transient opening samples, popup grabs, handle replacement, and failure recovery."""
        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, QT_QPA_PLATFORM="windows")
        environment.pop("QUE_AUDIT_COVERAGE", None)
        result = subprocess.run([sys.executable, str(repository / "test/stealth/surface_probe.py")], cwd=repository, env=environment, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertGreater(report["samples"], 0)
        self.assertFalse(report["failures"])
        self.assertGreaterEqual(len(report["completed"]), 22)

    def test_layout_dead_keys_space_and_altgr(self) -> None:
        """Translate real Windows layouts without changing the foreground keyboard layout."""
        native.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        native.GetKeyboardLayoutList.restype = ctypes.c_int
        native.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        native.LoadKeyboardLayoutW.restype = ctypes.c_void_p
        native.UnloadKeyboardLayout.argtypes = [ctypes.c_void_p]
        count = native.GetKeyboardLayoutList(0, None)
        layouts = (ctypes.c_void_p * count)()
        native.GetKeyboardLayoutList(count, layouts)
        existing = set(layouts)
        loaded = []
        try:
            international = native.LoadKeyboardLayoutW("00020409", 0)
            german = native.LoadKeyboardLayoutW("00000407", 0)
            self.assertTrue(international and german, "The native layout test cannot pass without both test layouts.")
            loaded.extend([international, german])
            with patch.object(native, "GetKeyState", return_value=0):
                translator = KeyTranslator()
            with patch.object(native, "GetKeyboardLayout", return_value=international):
                self.assertIsNone(translator.translate(0xDE, 0x28, 0, True, False, ()))
                event = translator.translate(ord("E"), 0x12, 0, True, False, ())
                self.assertIsNotNone(event)
                self.assertEqual(event.text(), "é")
                self.assertIsNone(translator.translate(0xDE, 0x28, 0, True, False, ()))
                self.assertEqual(translator.translate(0x20, 0x39, 0, True, False, ()).text(), "'")
                self.assertIsNone(translator.translate(0xDE, 0x28, 0, True, False, ()))
                self.assertIsNone(translator.translate(0x08, 0x0E, 0, True, False, ()))
                self.assertEqual(translator.translate(ord("E"), 0x12, 0, True, False, ()).text(), "e")
            with patch.object(native, "GetKeyboardLayout", return_value=german):
                event = translator.translate(ord("Q"), 0x10, 0, True, False, (0xA2, 0xA5))
                self.assertEqual(event.text(), "@")
                self.assertFalse(event.modifiers().value & 0x0C000000)
            translator.reset()
        finally:
            for layout in loaded:
                if layout not in existing:
                    native.UnloadKeyboardLayout(layout)


if __name__ == "__main__":
    unittest.main()
