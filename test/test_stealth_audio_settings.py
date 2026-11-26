"""Exercise audio device preferences without opening real audio streams."""

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from numpy.typing import NDArray
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication, QWidget
from qt_application import application
from test_main_window import WindowTestCase
from test_speaker import OUTPUT, FakeAudio

from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.settings import SettingsStore
from core.speaker import (
    DOWNLOADING_VOICE_STATUS,
    MISSING_VOICE_STATUS,
    MODEL_FILE,
    VOICE,
    VOICE_RATE,
    VOICE_SAMPLE,
    VOICES_FILE,
    _Output,
)
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


class SpeechSettingsTests(WindowTestCase):
    """Configure and preview speech through Voice settings with no physical audio device."""

    def test_volume_saves_on_release_without_starting_playback(self) -> None:
        """Preview the percentage as the slider moves, then persist the released value."""
        page = self.window.settings_page
        page.open_settings()
        tab = page.tab_bar
        tab.setCurrentIndex(
            next(i for i in range(tab.count()) if tab.tabText(i) == "Voice")
        )
        voice_tab = page.tab_pages.currentWidget()
        assert voice_tab is not None
        slider = page.speech_volume_slider
        self.assertTrue(voice_tab.isAncestorOf(slider))
        self.assertEqual((slider.minimum(), slider.maximum()), (0, 100))
        self.assertEqual(slider.value(), 100)
        self.assertEqual(page.speech_volume_value.text(), "100%")

        with patch("core.speaker.pyaudiowpatch.PyAudio") as audio:
            slider.setSliderDown(True)
            slider.setSliderPosition(35)
            self.assertEqual(page.speech_volume_value.text(), "35%")
            self.assertEqual(self.store.speech_volume(), 100)
            slider.setSliderDown(False)
            self.assertEqual(SettingsStore(self.store.path).speech_volume(), 35)

            wheel = MagicMock(spec=QWheelEvent)
            slider.wheelEvent(wheel)
            slider.wheelEvent(None)
            wheel.ignore.assert_called_once()
            self.assertEqual(slider.value(), 35)

            home = QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Home, Qt.KeyboardModifier.NoModifier
            )
            with patch.object(self.store, "save_speech_volume", side_effect=OSError):
                QApplication.sendEvent(slider, home)
            self.assertEqual(slider.value(), 35)
            self.assertEqual(page.speech_volume_value.text(), "35%")
            self.assertIn("speech volume could not be saved", page.save_status.text())

            QApplication.sendEvent(slider, home)
            page.open_settings()
            self.assertEqual(slider.value(), 0)
            self.assertEqual(page.speech_volume_value.text(), "0%")
            self.assertEqual(SettingsStore(self.store.path).speech_volume(), 0)
            audio.assert_not_called()
        self.assertIsNone(self.window.speaker.speech_thread)

    def test_volume_scales_queued_audio_on_the_existing_stream(self) -> None:
        """Scale both channels, including at mute, without reopening audio output."""
        with patch("core.speaker.pyaudiowpatch.PyAudio") as host:
            audio = host.return_value
            audio.get_host_api_info_by_type.return_value = {"defaultOutputDevice": 1}
            audio.get_device_info_by_index.return_value = {
                "defaultSampleRate": VOICE_RATE,
                "maxOutputChannels": 2,
                "index": 1,
            }
            output = _Output(self.window.speaker)
            self.addCleanup(output.close)
            fill = audio.open.call_args.kwargs["stream_callback"]
            output.push(np.full(4, 0.5, dtype=np.float32), 0)
            slider = self.window.settings_page.speech_volume_slider
            for volume, amplitude in ((100, 0.5), (50, 0.25), (0, 0.0), (100, 0.5)):
                with self.subTest(volume=volume):
                    slider.setValue(volume)
                    data, _flag = fill(None, 1, None, 0)
                    np.testing.assert_array_equal(
                        np.frombuffer(data, dtype=np.float32), [amplitude, amplitude]
                    )
            self.assertTrue(output.drained())
            audio.open.assert_called_once()

    def test_speed_saves_on_release_and_restores_on_failure(self) -> None:
        """Show the chosen multiplier and save only a committed change."""
        page = self.window.settings_page
        slider = page.speech_speed_slider
        self.assertEqual((slider.minimum(), slider.maximum()), (50, 200))
        self.assertEqual(slider.value(), 100)
        self.assertEqual(page.speech_speed_value.text(), "1.00×")
        with patch("core.speaker.pyaudiowpatch.PyAudio") as audio:
            slider.setSliderDown(True)
            slider.setSliderPosition(150)
            self.assertEqual(page.speech_speed_value.text(), "1.50×")
            self.assertEqual(self.store.speech_speed(), 100)
            slider.setSliderDown(False)
            self.assertEqual(SettingsStore(self.store.path).speech_speed(), 150)

            with patch.object(self.store, "save_speech_speed", side_effect=OSError):
                slider.setValue(200)
            self.assertEqual(slider.value(), 150)
            self.assertEqual(page.speech_speed_value.text(), "1.50×")
            self.assertIn("speech speed could not be saved", page.save_status.text())

            self.store.save_speech_speed(50)
            page.open_settings()
            self.assertEqual(slider.value(), 50)
            self.assertEqual(page.speech_speed_value.text(), "0.50×")
            audio.assert_not_called()
        self.assertIsNone(self.window.speaker.speech_thread)

    def test_sample_uses_the_configured_speed_volume_and_output(self) -> None:
        """Play one sentence on the chosen device with the configured speech settings."""
        speaker = self.window.speaker
        speaker.cache_dir.mkdir()
        for name in (MODEL_FILE, VOICES_FILE):
            (speaker.cache_dir / name).touch()
        voice = MagicMock()
        voice.create.return_value = (np.full(240, 0.5, np.float32), VOICE_RATE)
        speaker.voice = voice
        self.store.save_audio_device("speaker", str(OUTPUT["name"]))
        page = self.window.settings_page
        page.open_settings()
        tab = page.tab_bar
        tab.setCurrentIndex(
            next(i for i in range(tab.count()) if tab.tabText(i) == "Voice")
        )
        voice_tab = page.tab_pages.currentWidget()
        assert voice_tab is not None
        self.assertTrue(voice_tab.isAncestorOf(page.speech_speed_slider))
        self.assertTrue(voice_tab.isAncestorOf(page.voice_test_button))
        self.assertTrue(page.voice_test_button.isEnabled())
        page.speech_speed_slider.setValue(135)
        page.speech_volume_slider.setValue(50)

        with (
            patch.object(FakeAudio, "instances", []),
            patch("core.speaker.pyaudiowpatch.PyAudio", FakeAudio),
        ):
            page.voice_test_button.click()
            for _ in range(200):
                if FakeAudio.instances and FakeAudio.instances[0].terminated:
                    break
                time.sleep(0.01)
            (audio,) = FakeAudio.instances
            self.assertTrue(audio.terminated)
            self.assertEqual(audio.opened[0]["output_device_index"], OUTPUT["index"])
            self.assertEqual(audio.streams[0].peak, 0.25)
        voice.create.assert_called_once_with(
            VOICE_SAMPLE, voice=VOICE, speed=1.35, lang="en-us"
        )


class VoiceDownloadTests(WindowTestCase):
    """Exercise the cold voice flow through chat and Voice settings without real I/O."""

    def test_voice_download_requires_settings_and_can_retry(self) -> None:
        """Keep audio idle and contact the model host only from the download button."""
        window = self.window
        window.chat_area.add_message("Reply to read.", is_user=False)
        page = window.settings_page
        page.open_settings()
        tab = page.tab_bar
        tab.setCurrentIndex(
            next(i for i in range(tab.count()) if tab.tabText(i) == "Voice")
        )
        button = page.voice_download_button
        voice_tab = page.tab_pages.currentWidget()
        assert voice_tab is not None
        self.assertTrue(voice_tab.isAncestorOf(button))
        self.assertIs(page.speaker, window.speaker)
        self.assertEqual(button.text(), "Download")
        test_button = page.voice_test_button
        self.assertFalse(test_button.isEnabled())

        def download(_repo: str, **options: Any) -> None:
            """Write the pinned files into the test cache."""
            cache = Path(options["local_dir"])
            cache.mkdir()
            for name in (MODEL_FILE, VOICES_FILE):
                (cache / name).touch()

        with (
            patch(
                "huggingface_hub.snapshot_download", side_effect=OSError("offline")
            ) as snapshot,
            patch("core.speaker.pyaudiowpatch.PyAudio") as audio,
            patch("onnxruntime.InferenceSession") as session,
        ):
            test_button.click()
            window.speak_latest()
            self.assertEqual(window.voice_notice, MISSING_VOICE_STATUS)
            self.assertTrue(window.voice_notice_timer.isActive())
            snapshot.assert_not_called()
            window.voice_notice_timer.timeout.emit()
            self.assertEqual(window.voice_notice, "")

            button.click()
            page.open_settings()
            self.assertEqual(button.text(), "Downloading…")
            self.assertFalse(button.isEnabled())
            self.assertFalse(test_button.isEnabled())
            window.speak_latest()
            self.assertEqual(window.voice_notice, DOWNLOADING_VOICE_STATUS)
            self._wait_for_download()
            self.assertIn("offline", page.voice_download_status.text())
            self.assertTrue(button.isEnabled())
            self.assertFalse(test_button.isEnabled())

            snapshot.side_effect = download
            button.click()
            self._wait_for_download()
            self.assertEqual(button.text(), "Downloaded")
            self.assertFalse(button.isEnabled())
            self.assertTrue(test_button.isEnabled())
            self.assertEqual(
                page.voice_download_status.text(), "Downloaded Que's voice."
            )
            self.assertEqual(window.voice_notice, "")
            self.assertFalse(window.voice_notice_timer.isActive())
            self.assertIsNone(window.speaker.speech_thread)
            self.assertEqual(snapshot.call_count, 2)
            audio.assert_not_called()
            session.assert_not_called()

    def _wait_for_download(self) -> None:
        """Deliver the worker's queued result to the window and settings page."""
        for _ in range(100):
            self.app.processEvents()
            if not self.window.speaker.downloading:
                return
            time.sleep(0.01)
        self.fail("The voice download did not finish.")
