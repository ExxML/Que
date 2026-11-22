"""Tests for starting, stopping and failing audio capture.

Recording must start and stop exactly when asked, and every device failure must be reported
and still close the other source, so audio is never captured without the user knowing.
"""

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pyaudiowpatch  # type: ignore[import-untyped]
from qt_application import application

from core.settings import SettingsStore
from core.transcriber import Transcriber


class TranscriberTestCase(unittest.TestCase):
    """Create a transcriber over temporary settings, recording its failure reports."""

    def setUp(self) -> None:
        """Create the transcriber."""
        application()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.transcriber = Transcriber(
            SettingsStore(Path(directory.name) / "config.json")
        )
        self.failures: list[str] = []
        self.transcriber.session_failed.connect(
            lambda message, *_: self.failures.append(message)
        )
        self.stop_event = threading.Event()
        self.utterances: queue.Queue[tuple[str, Any] | None] = queue.Queue()

    def markers(self) -> int:
        """Count the completion markers queued for the transcription worker.

        Returns:
            int: The number of None entries.
        """
        count = 0
        while not self.utterances.empty():
            count += self.utterances.get_nowait() is None
        return count


class ToggleTests(TranscriberTestCase):
    """Start and stop recording on request."""

    def test_toggle_starts_then_stops(self) -> None:
        """Start both capture workers, then stop and announce it."""
        changes: list[bool] = []
        self.transcriber.recording_changed.connect(changes.append)
        with (
            patch.object(self.transcriber, "active_model", return_value="tiny"),
            patch("core.transcriber.threading.Thread") as thread,
        ):
            self.transcriber.toggle()
            self.transcriber.start()  # Already recording: nothing new starts
            self.transcriber.toggle()
        self.assertEqual(thread.call_count, 2)
        self.assertEqual(changes, [True, False])
        self.assertTrue(self.transcriber.stop_event.is_set())

    def test_no_model_while_downloading_explains_the_wait(self) -> None:
        """Ask the user to wait for a download instead of recording without a model."""
        statuses: list[str] = []
        self.transcriber.status_changed.connect(statuses.append)
        self.transcriber.downloading = "small"
        with patch.object(self.transcriber, "active_model", return_value=""):
            self.transcriber.start()
        self.assertFalse(self.transcriber.recording)
        self.assertIn("small model is still downloading", statuses[-1])

    def test_one_download_at_a_time(self) -> None:
        """Ignore a second download while one is running."""
        self.transcriber.downloading = "small"
        with patch("core.transcriber.threading.Thread") as thread:
            self.transcriber.download("medium")
        thread.assert_not_called()
        self.assertEqual(self.transcriber.downloading, "small")


class CaptureFailureTests(TranscriberTestCase):
    """Report every device failure and keep the other source's stream closed properly."""

    def capture(self, **patches: Any) -> None:
        """Run the capture loop for both sources with the given methods replaced.

        Args:
            **patches (Any): Transcriber methods to replace.
        """
        with (
            patch.object(pyaudiowpatch, "PyAudio"),
            patch.multiple(self.transcriber, **patches),
        ):
            self.transcriber._capture_sources(
                ("you", "them"), self.stop_event, self.utterances
            )

    def test_a_source_that_fails_mid_recording_is_closed_and_reported(self) -> None:
        """Close a failing source, report it, and keep the other until stopped."""
        polls = iter([OSError("unplugged"), None, None])

        def poll(capture: Any, *_args: Any) -> None:
            """Fail the microphone once, then stop the session.

            Args:
                capture (Any): The polled source.
            """
            result = next(polls)
            if capture.source == "you" and isinstance(result, Exception):
                raise result
            self.stop_event.set()

        close = MagicMock(side_effect=[OSError("busy"), None])
        self.capture(
            _open_source=MagicMock(
                side_effect=lambda _a, source, _n: MagicMock(source=source)
            ),
            _poll_source=poll,
            _close_source=close,
        )
        self.assertEqual(
            len(self.failures), 2
        )  # The poll failure, then the close failure
        self.assertIn("unplugged", self.failures[0])
        self.assertIn("busy", self.failures[1])
        self.assertEqual(close.call_count, 2)  # Both sources closed

    def test_finished_speech_goes_to_transcription(self) -> None:
        """Queue each completed utterance, labelled with its source."""

        def poll(capture: Any, *_args: Any) -> Any:
            """Return one utterance, then stop the session.

            Args:
                capture (Any): The polled source.

            Returns:
                Any: An utterance for the microphone, otherwise nothing.
            """
            self.stop_event.set()
            return np.ones(2, np.float32) if capture.source == "you" else None

        self.capture(
            _open_source=MagicMock(
                side_effect=lambda _a, source, _n: MagicMock(source=source)
            ),
            _poll_source=poll,
            _close_source=MagicMock(),
        )
        self.assertEqual(self.utterances.get_nowait()[0], "you")  # type: ignore[index]

    def test_a_source_that_cannot_open_is_reported(self) -> None:
        """Report a device that cannot open, and finish its transcription stream."""
        self.capture(_open_source=MagicMock(side_effect=OSError("no device")))
        self.assertEqual(len(self.failures), 2)
        self.assertEqual(self.markers(), 2)

    def test_audio_host_failure_reports_every_source(self) -> None:
        """Report both sources when the audio host itself fails."""
        with patch.object(pyaudiowpatch, "PyAudio", side_effect=OSError("no host")):
            self.transcriber._capture_sources(
                ("you", "them"), self.stop_event, self.utterances
            )
        self.assertEqual(len(self.failures), 2)
        self.assertEqual(self.markers(), 2)

    def test_close_failure_at_shutdown_is_reported(self) -> None:
        """Report a stream that fails to close when the session ends."""
        self.stop_event.set()
        self.capture(
            _open_source=MagicMock(
                side_effect=lambda _a, source, _n: MagicMock(source=source)
            ),
            _close_source=MagicMock(side_effect=OSError("stuck")),
        )
        self.assertEqual(len(self.failures), 2)


class StreamTests(TranscriberTestCase):
    """Queue device audio without blocking, and flush speech on close."""

    def test_callback_queues_audio_and_keeps_the_stream_running(self) -> None:
        """Queue each callback's audio and tell PortAudio to continue."""
        audio = MagicMock()
        audio.get_default_wasapi_loopback.return_value = {
            "maxInputChannels": 2,
            "defaultSampleRate": 48000,
            "index": 3,
        }
        capture = self.transcriber._open_source(audio, "them", "")
        callback = audio.open.call_args.kwargs["stream_callback"]
        self.assertEqual(
            callback(b"\x00" * 8),
            (None, int(pyaudiowpatch.paContinue)),
        )
        self.assertEqual(capture.chunks.get_nowait(), b"\x00" * 8)

    def test_close_flushes_unfinished_speech(self) -> None:
        """Send speech still in progress to transcription when a source closes."""
        capture = MagicMock(source="you")
        capture.segmenter.flush.return_value = np.ones(4, np.float32)
        self.transcriber._close_source(capture, self.utterances)
        source, utterance = self.utterances.get_nowait()  # type: ignore[misc]
        self.assertEqual(source, "you")
        self.assertEqual(len(utterance), 4)
        self.assertIsNone(self.utterances.get_nowait())
        capture.stream.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
