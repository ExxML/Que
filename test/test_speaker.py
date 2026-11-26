"""Tests that Que reads replies aloud as they stream, on the chosen device, and stops cleanly.

The user listens through earbuds during an interview, so speech must start the moment words
exist, wait without stuttering when it catches up, and never hold the audio device open while
Que is not reading.
"""

import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import numpy as np
from numpy.typing import NDArray
from qt_application import application

from core.speaker import (
    DEVICE_FAILED,
    DOWNLOADING_VOICE_STATUS,
    LOAD_FAILED,
    MISSING_VOICE_STATUS,
    MODEL_FILE,
    VOICE_REPO,
    VOICE_REVISION,
    VOICES_FILE,
    Speaker,
)

OUTPUT = {
    "index": 3,
    "name": "Earbuds",
    "maxInputChannels": 0,
    "maxOutputChannels": 2,
    "defaultSampleRate": 48000.0,
    "isLoopbackDevice": False,
}
DEFAULT = {**OUTPUT, "index": 1, "name": "Speakers"}
LOOPBACK = {
    **OUTPUT,
    "index": 4,
    "name": "Earbuds [Loopback]",
    "maxInputChannels": 2,
    "isLoopbackDevice": True,
}


class FakeVoice:
    """Stand in for Kokoro, returning a short tone per sentence."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def create(self, text: str, **_options: Any) -> tuple[NDArray[np.float32], int]:
        """Synthesize a sentence as 10 ms of tone.

        Args:
            text (str): The sentence.

        Returns:
            tuple[NDArray[np.float32], int]: The samples and their rate.
        """
        if text == "Boom.":
            raise ValueError("The phonemizer rejected the sentence.")
        self.texts.append(text)
        return np.full(240, 0.5, np.float32), 24000


class FakeStream:
    """Pull audio from the callback as fast as Windows would ask for it, but faster."""

    def __init__(self, callback: Callable[..., tuple[bytes, int]], frames: int) -> None:
        self.callback = callback
        self.frames = frames
        self.sound = 0  # Non-silent frames played
        self.silence = 0
        self.running = True
        self.thread = threading.Thread(target=self._pull, daemon=True)
        self.thread.start()

    def stop_stream(self) -> None:
        """Stop pulling audio."""
        self.running = False
        self.thread.join()

    def close(self) -> None:
        """Release nothing; the fake holds no device."""

    def _pull(self) -> None:
        """Ask for buffers until stopped, counting sound and silence."""
        while self.running:
            data, _flag = self.callback(None, self.frames, None, 0)
            samples = np.frombuffer(data, np.float32)
            if samples.any():
                self.sound += 1
            else:
                self.silence += 1
            time.sleep(0.001)


class FakeAudio:
    """Stand in for the PortAudio host with one output, its loopback and a default."""

    instances: ClassVar[list["FakeAudio"]] = []

    def __init__(self) -> None:
        self.streams: list[FakeStream] = []
        self.opened: list[dict[str, Any]] = []
        self.terminated = False
        self.fail_open = False
        FakeAudio.instances.append(self)

    def get_device_info_generator_by_host_api(self, host_api_type: int) -> Any:
        """List the WASAPI devices."""
        return iter([DEFAULT, OUTPUT, LOOPBACK])

    def get_host_api_info_by_type(self, _kind: int) -> dict[str, int]:
        """Describe WASAPI and its default output."""
        return {"defaultOutputDevice": 1, "defaultInputDevice": 0}

    def get_device_info_by_index(self, index: int) -> dict[str, Any]:
        """Return the default output."""
        return DEFAULT if index == 1 else OUTPUT

    def open(self, **options: Any) -> FakeStream:
        """Open an output stream that starts pulling audio at once."""
        if FakeAudio.fail_next:
            FakeAudio.fail_next = False
            raise OSError("Device in use.")
        self.opened.append(options)
        stream = FakeStream(options["stream_callback"], options["frames_per_buffer"])
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
        """Release the host."""
        self.terminated = True

    fail_next = False


class SpeakerTestCase(unittest.TestCase):
    """Build a speaker with a loaded fake voice and a fake PortAudio host."""

    def setUp(self) -> None:
        """Create the speaker and capture what it reports."""
        self.app = application()
        FakeAudio.instances = []
        FakeAudio.fail_next = False
        patcher = patch("core.speaker.pyaudiowpatch.PyAudio", FakeAudio)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MagicMock()
        self.store.audio_device.return_value = ""
        self.store.speech_volume.return_value = 100
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.speaker = Speaker(self.store, Path(directory.name))
        self.addCleanup(self.speaker.stop)
        self.voice = FakeVoice()
        self.speaker.voice = self.voice
        self.spoken: list[str] = []
        self.failures: list[str] = []
        self.downloads: list[str] = []
        self.model_notices: list[str] = []
        self.speaker.spoken.connect(self.spoken.append)
        self.speaker.failed.connect(self.failures.append)
        self.speaker.download_finished.connect(self.downloads.append)
        self.speaker.model_needed.connect(self.model_notices.append)
        self.reply = object()

    def wait_until(self, condition: Callable[[], bool], message: str) -> None:
        """Process events until a condition holds, failing after two seconds.

        Args:
            condition (Callable[[], bool]): The condition.
            message (str): What was expected.
        """
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail(message)

    def finished(self) -> bool:
        """Report whether reading ended and released the device.

        Returns:
            bool: Whether nothing is being read and every opened host was released.
        """
        return not self.speaker.active and all(
            audio.terminated for audio in FakeAudio.instances
        )


class ReadingTests(SpeakerTestCase):
    """Read replies as they stream."""

    def test_reads_a_finished_reply_and_releases_the_device(self) -> None:
        """Read every sentence once on the default output, then close it."""
        self.speaker.speak(self.reply, "One. Two.\n\n```\ncode\n```\nThree.", True)
        self.wait_until(self.finished, "Reading never finished.")
        self.app.processEvents()
        self.assertEqual(self.voice.texts, ["One.", "Two.", "Three."])
        self.assertEqual(self.spoken, ["One.", "Two.", "Three."])
        audio = FakeAudio.instances[0]
        self.assertEqual(audio.opened[0]["output_device_index"], 1)
        self.assertGreater(audio.streams[0].sound, 0)
        self.assertFalse(audio.streams[0].running)

    def test_pending_reply_starts_on_its_first_words(self) -> None:
        """Wait for a pending reply, read as words stream in, and finish with it."""
        self.speaker.speak(self.reply, "", False)
        time.sleep(0.1)
        self.assertEqual(
            FakeAudio.instances, []
        )  # Nothing opened before there is speech
        self.speaker.update(self.reply, "The answer", False)
        self.wait_until(
            lambda: self.voice.texts == ["The"], "The first word was not read."
        )
        self.speaker.update(self.reply, "The answer is 42. Then", False)
        self.wait_until(
            lambda: self.voice.texts == ["The", "answer is 42."],
            "Reading did not resume.",
        )
        self.speaker.update(
            object(), "Another reply.", True
        )  # Updates to other replies are ignored
        self.speaker.update(self.reply, "The answer is 42. Then done.", True)
        self.wait_until(self.finished, "Reading never finished.")
        self.assertEqual(self.voice.texts, ["The", "answer is 42.", "Then done."])

    def test_restart_reads_from_the_start_again(self) -> None:
        """Start over from the beginning when asked again, unpausing."""
        self.speaker.speak(self.reply, "First. Second.", False)
        self.wait_until(
            lambda: self.speaker.output is not None, "Reading never started."
        )
        self.speaker.toggle_pause()
        self.speaker.speak(self.reply, "First. Second. Third.", True)
        self.assertFalse(self.speaker.paused)
        self.wait_until(self.finished, "Reading never finished.")
        self.assertEqual(self.voice.texts[-3:], ["First.", "Second.", "Third."])

    def test_offset_reads_from_a_line(self) -> None:
        """Read from an offset, as a click on a line asks."""
        text = "Skipped line.\nRead this line."
        self.speaker.speak(self.reply, text, True, text.index("Read"))
        self.wait_until(self.finished, "Reading never finished.")
        self.assertEqual(self.voice.texts, ["Read this line."])

    def test_unsayable_sentence_is_skipped(self) -> None:
        """Skip a sentence the voice rejects and carry on."""
        self.speaker.speak(self.reply, "Boom. After.", True)
        self.wait_until(self.finished, "Reading never finished.")
        self.assertEqual(self.voice.texts, ["After."])


class ControlTests(SpeakerTestCase):
    """Pause, resume and stop."""

    def test_pause_plays_silence_until_resumed(self) -> None:
        """Hold the queued sentence while paused and play it after resuming."""
        self.speaker.speak(self.reply, "Waiting. ", False)
        self.speaker.toggle_pause()
        self.wait_until(
            lambda: bool(FakeAudio.instances and FakeAudio.instances[0].streams),
            "No stream.",
        )
        stream = FakeAudio.instances[0].streams[0]
        time.sleep(0.05)
        self.assertEqual(stream.sound, 0)
        self.speaker.toggle_pause()
        self.wait_until(lambda: stream.sound > 0, "Resuming played nothing.")

    def test_growing_sentence_waits_for_queued_audio(self) -> None:
        """Hold a sentence the stream may extend until what is queued has played."""
        self.speaker.speak(self.reply, "First. Second part", False)
        self.speaker.toggle_pause()
        self.wait_until(
            lambda: self.voice.texts == ["First."], "Reading never started."
        )
        time.sleep(0.1)
        self.assertEqual(self.voice.texts, ["First."])  # "Second" may still grow
        self.speaker.toggle_pause()
        self.wait_until(
            lambda: self.voice.texts == ["First.", "Second"],
            "The open sentence was never read.",
        )

    def test_stop_during_synthesis_drops_the_sentence(self) -> None:
        """Play nothing from a sentence synthesized after reading stopped."""
        create = self.voice.create

        def stop_then_create(
            text: str, **options: Any
        ) -> tuple[NDArray[np.float32], int]:
            """Stop reading while this sentence is being synthesized."""
            self.speaker.stop()
            return create(text, **options)

        self.voice.create = stop_then_create  # type: ignore[method-assign]
        self.speaker.speak(self.reply, "Dropped.", True)
        self.wait_until(lambda: not self.speaker.active, "Reading never stopped.")
        time.sleep(0.05)
        self.app.processEvents()
        self.assertEqual(self.spoken, [])
        self.assertEqual(FakeAudio.instances, [])

    def test_pause_does_nothing_when_idle(self) -> None:
        """Ignore pause while nothing is being read."""
        self.speaker.toggle_pause()
        self.assertFalse(self.speaker.paused)

    def test_stop_silences_and_releases_the_device(self) -> None:
        """Drop queued audio and close the device at once."""
        self.speaker.speak(self.reply, "One sentence. Another one. ", False)
        self.wait_until(lambda: bool(self.voice.texts), "Reading never started.")
        self.speaker.stop()
        self.wait_until(self.finished, "Stopping kept the device open.")
        self.speaker.update(
            self.reply, "More text.", True
        )  # A stopped reply is no longer followed
        self.assertFalse(self.speaker.active)


class DeviceTests(SpeakerTestCase):
    """Play on the device chosen in Settings → Audio."""

    def test_saved_device_is_used(self) -> None:
        """Open the saved output by name, never its loopback twin."""
        self.store.audio_device.return_value = "Earbuds"
        self.speaker.speak(self.reply, "Hello.", True)
        self.wait_until(self.finished, "Reading never finished.")
        self.store.audio_device.assert_called_with("speaker")
        self.assertEqual(FakeAudio.instances[0].opened[0]["output_device_index"], 3)

    def test_missing_device_reports_the_failure(self) -> None:
        """Explain how to fix a disconnected saved device."""
        self.store.audio_device.return_value = "Unplugged"
        self.speaker.speak(self.reply, "Hello.", True)
        self.wait_until(lambda: self.failures == [DEVICE_FAILED], "No failure shown.")
        self.assertTrue(self.finished())

    def test_device_that_fails_to_open_reports_the_failure(self) -> None:
        """Explain a device that refuses the stream."""
        FakeAudio.fail_next = True
        self.speaker.speak(self.reply, "Hello.", True)
        self.wait_until(lambda: self.failures == [DEVICE_FAILED], "No failure shown.")


class VoiceTests(SpeakerTestCase):
    """Download the voice explicitly and load it offline when speech starts."""

    def setUp(self) -> None:
        """Start without a loaded voice."""
        super().setUp()
        self.speaker.voice = None

    def test_missing_voice_requests_download_without_starting_speech(self) -> None:
        """Keep speech and network idle when only part of the model is present."""
        (self.speaker.cache_dir / MODEL_FILE).write_bytes(b"")
        with patch("huggingface_hub.snapshot_download") as download:
            self.speaker.speak(self.reply, "Hello.", True)
        download.assert_not_called()
        self.assertEqual(self.model_notices, [MISSING_VOICE_STATUS])
        self.assertIsNone(self.speaker.speech_thread)
        self.assertEqual(FakeAudio.instances, [])

    def test_download_then_load(self) -> None:
        """Download the pinned files once and wait for a fresh speech request."""
        release = threading.Event()
        cache = self.speaker.cache_dir

        def download(_repo: str, **_options: Any) -> None:
            """Write the model files where the speaker expects them."""
            if not release.wait(2):
                raise TimeoutError("Download fixture waited too long.")
            for name in (MODEL_FILE, VOICES_FILE):
                (cache / name).write_bytes(b"")

        with (
            patch(
                "huggingface_hub.snapshot_download", side_effect=download
            ) as snapshot,
            patch("onnxruntime.InferenceSession") as session,
            patch("kokoro_onnx.Kokoro.from_session", return_value=self.voice) as load,
        ):
            self.assertTrue(self.speaker.download())
            try:
                self.assertFalse(self.speaker.download())
                self.speaker.speak(self.reply, "Try while downloading.", True)
                self.assertEqual(self.model_notices, [DOWNLOADING_VOICE_STATUS])
            finally:
                release.set()
            self.wait_until(lambda: bool(self.downloads), "No download result.")
            self.assertEqual(self.downloads, [""])
            self.assertTrue(self.speaker.model_downloaded())
            self.assertFalse(self.speaker.downloading)
            self.assertFalse(self.speaker.download())
            snapshot.assert_called_once_with(
                VOICE_REPO,
                revision=VOICE_REVISION,
                local_dir=str(cache),
                allow_patterns=[MODEL_FILE, VOICES_FILE],
            )
            session.assert_not_called()
            self.assertIsNone(self.speaker.speech_thread)
            self.assertEqual(FakeAudio.instances, [])
            self.speaker.speak(self.reply, "Hello.", True)
            self.wait_until(self.finished, "Reading never finished.")
            self.speaker.speak(self.reply, "Again.", True)
            self.wait_until(
                lambda: self.finished() and "Again." in self.voice.texts,
                "Second read failed.",
            )
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(session.call_count, 1)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(self.voice.texts, ["Hello.", "Again."])

    def test_download_failure_is_reported(self) -> None:
        """Surface download errors and allow another attempt with an incomplete cache."""
        with patch("huggingface_hub.snapshot_download", side_effect=OSError()):
            self.assertTrue(self.speaker.download())
            self.wait_until(lambda: bool(self.downloads), "No failure shown.")
        self.assertEqual(self.downloads, ["OSError"])
        with patch("huggingface_hub.snapshot_download"):
            self.assertTrue(self.speaker.download())
            self.wait_until(lambda: len(self.downloads) == 2, "No retry result.")
        self.assertIn("both voice files", self.downloads[-1])
        self.assertFalse(self.speaker.model_downloaded())
        self.assertFalse(self.speaker.downloading)
        self.assertIsNone(self.speaker.speech_thread)

    def test_load_failure_is_reported(self) -> None:
        """Explain a voice that cannot load."""
        for name in (MODEL_FILE, VOICES_FILE):
            (self.speaker.cache_dir / name).write_bytes(b"")
        with patch(
            "onnxruntime.InferenceSession", side_effect=RuntimeError("bad model")
        ):
            self.speaker.speak(self.reply, "Hello.", True)
            self.wait_until(lambda: self.failures == [LOAD_FAILED], "No failure shown.")
        self.assertEqual(self.downloads, [])


if __name__ == "__main__":
    unittest.main()
