"""Exercise audio device preferences without opening real audio streams."""

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from numpy.typing import NDArray
from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.settings import SettingsStore
from core.transcriber import Transcriber
from ui.window.settings_page import SettingsPage

MIC = "USB microphone"
HEADSET = "USB headphones [Loopback]"


class AudioSettingsTests(unittest.TestCase):
    """Verify selection, refresh, persistence and session routing through Settings."""

    def setUp(self) -> None:
        """Create the production Settings page with isolated preferences and an audio host stub."""
        self.app = application()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = SettingsStore(root / "config.json")
        self.transcriber = Transcriber(self.store)
        self.window = QWidget()
        self.addCleanup(self.window.close)
        self.page = SettingsPage(
            self.store,
            ClaudeBridge(),
            self.transcriber,
            ChatHistory(root / "history.json"),
            self.window,
        )
        refresh = patch.object(self.page.catalog, "refresh", return_value=1)
        refresh.start()
        self.addCleanup(refresh.stop)
        self.audio = MagicMock()
        self.audio.__enter__.return_value = self.audio
        host = patch("core.transcriber.pyaudiowpatch.PyAudio", return_value=self.audio)
        self.host = host.start()
        self.addCleanup(host.stop)
        self.audio.get_device_info_generator_by_host_api.return_value = [
            {
                "name": MIC,
                "index": 11,
                "isLoopbackDevice": False,
                "maxInputChannels": 1,
                "defaultSampleRate": 16000,
            },
            {
                "name": HEADSET,
                "index": 19,
                "isLoopbackDevice": True,
                "maxInputChannels": 2,
                "defaultSampleRate": 48000,
            },
            {
                "name": "Playback endpoint",
                "index": 18,
                "isLoopbackDevice": False,
                "maxInputChannels": 0,
                "maxOutputChannels": 2,
            },
        ]

    def test_open_refresh_and_persist_independent_sources(self) -> None:
        """Discover compatible devices on demand and save each selection independently."""
        self.page.open_settings()
        self.host.assert_not_called()
        self.assertEqual(self.store.audio_device("you"), "")
        self.page.tab_bar.setCurrentIndex(self.page.audio_tab_index)
        self._wait_for_devices()
        you, them, speaker = self.page.audio_combos.values()
        self.assertEqual(
            [you.itemText(i) for i in range(you.count())], ["Windows default", MIC]
        )
        self.assertEqual(
            [speaker.itemText(i) for i in range(speaker.count())],
            ["Windows default", "Playback endpoint"],
        )
        speaker.setCurrentIndex(1)
        self.assertEqual(
            SettingsStore(self.store.path).audio_device("speaker"), "Playback endpoint"
        )
        self.assertEqual(
            [them.itemText(i) for i in range(them.count())],
            ["Windows default", "USB headphones"],
        )
        with patch.object(self.store, "save_audio_device") as save:
            you.currentIndexChanged.emit(0)
        save.assert_not_called()
        you.setCurrentIndex(1)
        them.setCurrentIndex(1)
        reloaded = SettingsStore(self.store.path)
        self.assertEqual(
            (reloaded.audio_device("you"), reloaded.audio_device("them")),
            (MIC, HEADSET),
        )
        self.page.audio_refresh.click()
        self.page.open_settings()  # Reopening while a refresh is already running.
        self._wait_for_devices()
        self.assertEqual(self.host.call_count, 2)
        self.assertEqual((you.currentData(), them.currentData()), (MIC, HEADSET))
        you.setCurrentIndex(0)
        self.assertEqual(
            (self.store.audio_device("you"), self.store.audio_device("them")),
            ("", HEADSET),
        )
        self.audio.open.assert_not_called()

    def test_unplugged_device_is_retained_and_restored_on_reconnect(self) -> None:
        """Keep the preference visible when unplugged and restore it when the device returns."""
        self.store.save_audio_device("you", MIC)
        devices = self.audio.get_device_info_generator_by_host_api.return_value
        self.audio.get_device_info_generator_by_host_api.return_value = []
        self.page.tab_bar.setCurrentIndex(self.page.audio_tab_index)
        self._wait_for_devices()
        combo = self.page.audio_combos["you"]
        self.assertEqual(combo.currentText(), f"{MIC} (unavailable)")
        self.assertEqual(self.store.audio_device("you"), MIC)
        self.assertIn("No audio devices", self.page.audio_status.text())
        self.audio.get_device_info_generator_by_host_api.return_value = devices
        self.page.audio_refresh.click()
        self._wait_for_devices()
        self.assertEqual(combo.currentText(), MIC)
        self.assertEqual(self.page.audio_status.text(), "")

    def test_refresh_failure_retains_choices_and_save_failure_rolls_back(self) -> None:
        """Keep the usable list on refresh failure and roll back an unwritable preference."""
        self.page.tab_bar.setCurrentIndex(self.page.audio_tab_index)
        self._wait_for_devices()
        combo = self.page.audio_combos["you"]
        self.audio.get_device_info_generator_by_host_api.side_effect = OSError(
            "Unavailable"
        )
        self.page.audio_refresh.click()
        self._wait_for_devices()
        self.assertIn("Could not refresh", self.page.audio_status.text())
        self.assertEqual(combo.itemData(1), MIC)
        with patch.object(self.store, "save_audio_device", side_effect=OSError):
            combo.setCurrentIndex(1)
        self.assertEqual(combo.currentIndex(), 0)
        self.assertEqual(self.store.audio_device("you"), "")
        self.assertIn("could not be saved", self.page.save_status.text())

    def test_duplicate_device_names_are_ambiguous(self) -> None:
        """Avoid offering or opening a microphone when identical names make it ambiguous."""
        devices = self.audio.get_device_info_generator_by_host_api.return_value
        devices.append({**devices[0], "index": 12})
        self.page.tab_bar.setCurrentIndex(self.page.audio_tab_index)
        self._wait_for_devices()
        self.assertEqual(self.page.audio_combos["you"].count(), 1)
        self.assertIn("unique names", self.page.audio_status.text())
        with self.assertRaises(OSError):
            self.transcriber._open_source(self.audio, "you", MIC)
        self.audio.open.assert_not_called()

    def test_recording_snapshots_selections_and_resolves_current_indices(self) -> None:
        """Start a recording with the saved devices even if indices or preferences change."""
        self.store.save_audio_device("you", MIC)
        self.store.save_audio_device("them", HEADSET)
        with (
            patch.object(self.transcriber, "active_model", return_value="tiny.en"),
            patch("core.transcriber.threading.Thread") as worker,
        ):
            self.transcriber.start()
        capture = worker.call_args.kwargs
        self.store.save_audio_device("you", "")
        self.store.save_audio_device("them", "")
        capture["args"][
            1
        ].set()  # Exercise stream acquisition and cleanup without polling.
        capture["target"](*capture["args"])
        self.assertEqual(
            [
                call.kwargs["input_device_index"]
                for call in self.audio.open.call_args_list
            ],
            [11, 19],
        )
        self.audio.get_default_wasapi_loopback.assert_not_called()
        self.audio.get_host_api_info_by_type.assert_not_called()
        self.transcriber.stop()

    def test_unavailable_source_does_not_block_the_other(self) -> None:
        """Warn about the disconnected device and open only the remaining requested source."""
        statuses: list[str] = []
        self.transcriber.failed.connect(
            lambda message, _fatal: statuses.append(message)
        )
        stop = threading.Event()
        stop.set()
        utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None] = queue.Queue()
        self.transcriber._capture_sources(
            ("you", "them"),
            stop,
            utterances,
            {"you": "Disconnected input", "them": HEADSET},
        )
        self.audio.open.assert_called_once()
        self.assertEqual(self.audio.open.call_args.kwargs["input_device_index"], 19)
        self.assertIn("You audio is unavailable", statuses[0])
        self.assertIn("Settings → Audio", statuses[0])
        self.assertEqual(
            [utterances.get_nowait(), utterances.get_nowait()], [None, None]
        )

    def _wait_for_devices(self) -> None:
        """Process queued device results on the Qt thread within a bounded interval."""
        for _ in range(100):
            self.app.processEvents()
            if self.page.audio_refresh.isEnabled():
                return
            time.sleep(0.01)
        self.fail("The audio device list did not refresh.")
