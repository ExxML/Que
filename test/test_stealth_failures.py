"""Exercise controlled provider, screenshot and transcription failures without live traffic."""

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
from google import genai
from google.genai import types
from numpy.typing import NDArray
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication

from core.ai_receiver import AIReceiver
from core.ai_sender import AISender
from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.model_catalog import ModelOption
from core.screenshot_manager import ScreenshotManager
from core.settings import SettingsStore
from core.transcriber import Transcriber


class ControlledFailureTests(unittest.TestCase):
    """Verify error completion evidence using isolated fixtures instead of exhausting quotas."""

    def setUp(self) -> None:
        """Create a Qt application for real signal delivery."""
        self.app = QApplication.instance() or QApplication([])

    def test_provider_auth_quota_and_server_errors(self) -> None:
        """Exercise real SDK error responses without any live quota consumption."""
        for status in (401, 403, 429, 503):
            with self.subTest(status=status):

                def respond(
                    _request: httpx.Request, status: int = status
                ) -> httpx.Response:
                    """Return one controlled provider failure."""
                    return httpx.Response(
                        status,
                        json={
                            "error": {
                                "code": str(status),
                                "message": "synthetic provider failure",
                            }
                        },
                    )

                client = genai.Client(
                    api_key="dummy",
                    http_options=types.HttpOptions(
                        httpx_client=httpx.Client(
                            transport=httpx.MockTransport(respond)
                        ),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                )
                self.addCleanup(client.close)
                settings = MagicMock(
                    **{"gemini_key.return_value": "", "provider.return_value": "gemini"}
                )
                sender = AISender(settings, client)
                sender.set_models(
                    (ModelOption("gemini-2.5-flash", "Fixture", ("default",)),)
                )
                sender.previous_interaction_id = "synthetic-prior"
                with self.assertRaises(RuntimeError):
                    sender.send_message(
                        sender.prepare_request("Synthetic request"),
                        lambda _: None,
                        threading.Event(),
                    )
                self.assertEqual(sender.previous_interaction_id, "synthetic-prior")

    def test_timeout_and_connection_failure(self) -> None:
        """Keep timeout and disconnected requests from committing a successful turn."""
        for failure in (
            httpx.ReadTimeout("synthetic timeout"),
            httpx.ConnectError("synthetic connection error"),
        ):
            settings = MagicMock(
                **{"gemini_key.return_value": "", "provider.return_value": "gemini"}
            )
            client = MagicMock()
            client.interactions.create.side_effect = failure
            sender = AISender(settings, client)
            sender.set_models(
                (ModelOption("gemini-2.5-flash", "Fixture", ("default",)),)
            )
            sender.previous_interaction_id = "synthetic-prior"
            with self.assertRaises(RuntimeError):
                sender.send_message(
                    sender.prepare_request("Synthetic request"),
                    lambda _: None,
                    threading.Event(),
                )
            self.assertEqual(sender.previous_interaction_id, "synthetic-prior")

    def test_screenshot_failure_has_no_attachment(self) -> None:
        """Invoke the real screenshot failure path without capturing the desktop."""
        manager = ScreenshotManager.__new__(ScreenshotManager)
        QObject.__init__(manager)
        with tempfile.TemporaryDirectory() as directory:
            manager.screenshots_dir = Path(directory)
            manager.screenshot_count = 0
            manager.pending_paths = []
            added = MagicMock()
            manager.screenshot_added.connect(added)
            with patch(
                "core.screenshot_manager.mss.mss",
                side_effect=OSError("synthetic capture failure"),
            ):
                self.assertEqual(manager.take_screenshot(), "")
            self.assertEqual(manager.pending_paths, [])
            self.assertEqual(list(Path(directory).iterdir()), [])
            added.assert_not_called()

    def test_download_and_cold_load_errors(self) -> None:
        """Exercise controlled model download and load failures with a private cache."""
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Transcriber(SettingsStore(Path(directory) / "config.json"))
            transcriber.cache_dir = Path(directory) / "cache"
            completed = MagicMock()
            transcriber.download_finished.connect(completed)
            with patch(
                "faster_whisper.download_model",
                side_effect=OSError("synthetic offline download"),
            ):
                transcriber._download("tiny.en")
            completed.assert_called_once_with("tiny.en", "synthetic offline download")
            with (
                patch(
                    "faster_whisper.WhisperModel",
                    side_effect=OSError("synthetic missing model"),
                ),
                self.assertRaises(OSError),
            ):
                transcriber._load_model("tiny.en")
            self.assertIsNone(transcriber.model)

    def test_capture_device_failure_reports_and_finishes_both_sources(self) -> None:
        """Require unavailable microphone/loopback errors and queue completion without live devices."""
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Transcriber(SettingsStore(Path(directory) / "config.json"))
            failed = MagicMock()
            transcriber.failed.connect(failed)
            for source in ("you", "them"):
                utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None] = (
                    queue.Queue()
                )
                with patch(
                    "core.transcriber.pyaudiowpatch.PyAudio",
                    side_effect=OSError("synthetic unavailable device"),
                ):
                    transcriber._capture(source, threading.Event(), utterances)
                self.assertIsNone(utterances.get_nowait())
                self.assertTrue(utterances.empty())
                self.assertIn("synthetic unavailable device", failed.call_args.args[0])
                self.assertFalse(failed.call_args.args[1])
            self.assertEqual(failed.call_count, 2)

    def test_transcription_failure_then_recovery_and_fatal_model_error(self) -> None:
        """Exercise fatal loading and recoverable inference errors with actual queue termination."""
        with tempfile.TemporaryDirectory() as directory:
            transcriber = Transcriber(SettingsStore(Path(directory) / "config.json"))
            failed, transcribed = MagicMock(), MagicMock()
            transcriber.failed.connect(failed)
            transcriber.transcribed.connect(transcribed)
            utterances: queue.Queue[tuple[str, NDArray[np.float32]] | None] = (
                queue.Queue()
            )
            with patch.object(
                transcriber, "_load_model", side_effect=OSError("fixture load failure")
            ):
                transcriber._transcribe_loop(utterances, "tiny.en")
            self.assertTrue(failed.call_args.args[1])
            failed.reset_mock()
            for _ in range(2):
                utterances.put(("you", np.zeros(320, np.float32)))
            utterances.put(None)
            utterances.put(None)
            model = MagicMock()
            model.transcribe.side_effect = [
                RuntimeError("fixture inference failure"),
                ([MagicMock(text="Recovered fixture phrase")], None),
            ]
            with patch.object(transcriber, "_load_model", return_value=model):
                transcriber._transcribe_loop(utterances, "tiny.en")
            failed.assert_called_once_with(
                "Transcription failed: fixture inference failure", False
            )
            transcribed.assert_called_once_with("you", "Recovered fixture phrase")
            self.assertTrue(utterances.empty())

    def test_receiver_error_delivery_and_cancelled_failure(self) -> None:
        """Deliver active provider errors while rejecting stale and cancelled completions."""
        sender, chat = MagicMock(), MagicMock()
        sender.send_message.side_effect = RuntimeError("fixture provider error")
        receiver = AIReceiver(sender, chat)
        receiver._run(MagicMock(), receiver.stream_id, threading.Event())
        chat.show_stream_error.assert_called_once_with(
            "Error generating response: fixture provider error"
        )
        chat.reset_mock()
        cancelled = threading.Event()
        cancelled.set()
        receiver._run(MagicMock(), receiver.stream_id, cancelled)
        receiver._on_response_error("stale", receiver.stream_id - 1)
        chat.show_stream_error.assert_not_called()
        sender.commit_interaction.assert_not_called()

    def test_corrupt_history_with_unwritable_backup(self) -> None:
        """Retain corrupt input when both parsing and backup rotation fail."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text("fixture invalid JSON", encoding="utf-8")
            with patch.object(Path, "replace", side_effect=OSError("fixture denied")):
                history = ChatHistory(path)
            self.assertEqual(history.conversations(), [])
            self.assertEqual(path.read_text(encoding="utf-8"), "fixture invalid JSON")

    def test_malformed_claude_discovery_and_termination_failure(self) -> None:
        """Observe malformed auth output and process termination errors without invoking Claude."""
        bridge = ClaudeBridge()
        with (
            patch.object(bridge, "_command", return_value=["fixture"]),
            patch(
                "core.claude_bridge.subprocess.run",
                return_value=MagicMock(stdout="invalid JSON", returncode=0),
            ),
            self.assertRaises(RuntimeError),
        ):
            bridge.discover()
        process = MagicMock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError("fixture termination failure")
        bridge.processes.add(process)
        bridge.close()
        self.assertTrue(bridge.closed.is_set())
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
