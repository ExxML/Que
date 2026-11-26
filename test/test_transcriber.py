"""Verify utterance segmentation, transcript delivery tracking, and prompt composition."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np
from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.ai_sender import InteractionRequest
from core.settings import WHISPER_MODELS, SettingsStore
from core.transcriber import (
    CHUNK_SECONDS,
    MODEL_FILES,
    ONNX_THREADS,
    SAMPLE_RATE,
    VAD_CONTEXT,
    VAD_DIR,
    VAD_FILE,
    VAD_FRAME,
    VAD_REPO,
    Transcriber,
    _Resampler,
    _Segmenter,
    _SileroVad,
    _to_mono_16k,
)
from ui.window.transcript_panel import TranscriptPanel

CHUNK = int(SAMPLE_RATE * CHUNK_SECONDS)
LOUD = np.full(CHUNK, 0.1, np.float32)
QUIET = np.zeros(CHUNK, np.float32)
MODEL = WHISPER_MODELS[0]


class SegmenterTests(unittest.TestCase):
    """Split speech on silence without dropping or emitting noise."""

    def test_speech_then_silence_yields_one_utterance(self) -> None:
        """Return the utterance, including pre-roll, once enough quiet follows."""
        segmenter = _Segmenter()
        quiet = np.zeros(CHUNK, np.float32)
        loud = np.full(CHUNK, 0.1, np.float32)
        self.assertIsNone(segmenter.feed(quiet))
        for _ in range(5):
            self.assertIsNone(segmenter.feed(loud))
        self.assertTrue(segmenter.speaking)
        utterance = None
        for _ in range(10):
            utterance = utterance if utterance is not None else segmenter.feed(quiet)
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertFalse(segmenter.speaking)
        self.assertGreaterEqual(utterance.size, 6 * CHUNK)  # pre-roll + speech

    def test_missing_audio_counts_as_silence(self) -> None:
        """End speech on a silent loopback device that delivers no data."""
        segmenter = _Segmenter()
        for _ in range(5):
            segmenter.feed(np.full(CHUNK, 0.1, np.float32))
        results = [
            segmenter.feed(np.zeros(0, np.float32), CHUNK_SECONDS) for _ in range(8)
        ]
        self.assertTrue(any(result is not None for result in results))

    def test_short_click_is_ignored(self) -> None:
        """Drop utterances shorter than the minimum length."""
        segmenter = _Segmenter()
        segmenter.feed(np.full(CHUNK, 0.1, np.float32))
        self.assertIsNone(segmenter.flush())

    def test_longer_speech_ends_on_shorter_pauses(self) -> None:
        """Need a long pause early in a line and only a brief one once it runs long."""
        for speech_chunks, pause_chunks in ((10, 5), (40, 3), (70, 1)):
            with self.subTest(speech_seconds=speech_chunks * CHUNK_SECONDS):
                segmenter = _Segmenter()
                for _ in range(speech_chunks):
                    self.assertIsNone(segmenter.feed(LOUD))
                for _ in range(pause_chunks - 1):
                    self.assertIsNone(segmenter.feed(QUIET))
                utterance = segmenter.feed(QUIET)
                assert utterance is not None
                self.assertEqual(utterance.size, (speech_chunks + pause_chunks) * CHUNK)

    def test_unbroken_speech_splits_at_its_least_speechlike_chunk(self) -> None:
        """Cut a 10 s monologue at its weakest recent chunk and keep every sample."""
        scores = [0.9] * 120
        scores[95] = 0.4  # Still speech, so no pause ever ends the line
        vad = MagicMock()
        vad.score.side_effect = scores
        segmenter = _Segmenter(vad)
        chunks = [np.full(CHUNK, index, np.float32) for index in range(120)]
        lines = [
            line for chunk in chunks if (line := segmenter.feed(chunk)) is not None
        ]
        self.assertEqual(lines[0].size, 96 * CHUNK)
        self.assertEqual(lines[0][-1], 95)  # Ends with the weakest chunk
        self.assertTrue(segmenter.speaking)
        rest = segmenter.flush()
        assert rest is not None
        np.testing.assert_array_equal(
            np.concatenate([*lines, rest]), np.concatenate(chunks)
        )


class AudioPreparationTests(unittest.TestCase):
    """Feed recognition clean 16 kHz audio and per-source speech probabilities."""

    def test_silero_vad_scores_whole_frames_and_keeps_state(self) -> None:
        """Run each completed 512-sample frame once with its context and recurrent state."""
        session = MagicMock()
        session.run.side_effect = [
            (
                np.array([[probability]], np.float32),
                np.full((2, 1, 128), index, np.float32),
            )
            for index, probability in enumerate((0.2, 0.8, 0.4, 0.1))
        ]
        vad = _SileroVad(session)
        samples = np.arange(CHUNK, dtype=np.float32)
        self.assertAlmostEqual(vad.score(samples), 0.8)
        self.assertEqual(
            session.run.call_count, 3
        )  # 1600 samples: three frames, 64 left over
        inputs = session.run.call_args.args[1]
        self.assertEqual(inputs["input"].shape, (1, VAD_CONTEXT + VAD_FRAME))
        np.testing.assert_array_equal(
            inputs["input"][0, :VAD_CONTEXT], samples[960:1024]
        )
        self.assertEqual(inputs["state"][0, 0, 0], 1)  # The previous frame's state
        self.assertAlmostEqual(vad.score(samples[:100]), 0.8)  # No whole frame yet
        self.assertAlmostEqual(vad.score(samples[:400]), 0.1)

    def test_resampler_removes_aliasing_and_keeps_speech(self) -> None:
        """Keep a 1 kHz tone, remove a 12 kHz tone, and match whole-signal filtering in chunks."""
        time = np.arange(48000) / 48000

        def level(frequency: int) -> float:
            """Return the peak of a tone after downsampling, past the filter's start-up."""
            tone = np.sin(2 * np.pi * frequency * time).astype(np.float32)
            return float(np.abs(_Resampler(48000).resample(tone)[200:]).max())

        self.assertGreater(level(1000), 0.95)
        self.assertLess(level(12000), 0.05)
        signal = np.random.default_rng(1).standard_normal(48000).astype(np.float32)
        chunked = _Resampler(48000)
        np.testing.assert_allclose(
            np.concatenate([chunked.resample(part) for part in np.split(signal, 10)]),
            _Resampler(48000).resample(signal),
            atol=1e-5,
        )
        self.assertIs(_Resampler(SAMPLE_RATE).resample(signal), signal)
        self.assertEqual(_Resampler(8000).resample(signal[:800]).size, 1600)

    def test_stereo_frames_become_mono(self) -> None:
        """Average the channels before resampling."""
        frames = np.array([[0.2, 0.4], [0.6, 0.8]], np.float32).tobytes()
        mono = _to_mono_16k(frames, 2, _Resampler(SAMPLE_RATE))
        np.testing.assert_allclose(mono, [0.3, 0.7])


class ModelFilesTests(unittest.TestCase):
    """Download, detect and load the speech models only from Que's own cache."""

    def setUp(self) -> None:
        """Point a transcriber at an empty private cache."""
        self.app = application()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        directory = Path(self.directory.name)
        self.store = SettingsStore(directory / "config.json")
        self.transcriber = Transcriber(self.store)
        self.transcriber.cache_dir = directory / "cache"
        self.transcriber.cache_dir.mkdir()

    def test_saved_model_is_active_only_with_a_complete_cache(self) -> None:
        """Use the saved model when its files and the VAD are present."""
        self.store.save_whisper_model(MODEL)
        self.assertEqual(self.transcriber.downloaded_models(), [])
        self.assertEqual(self.transcriber.active_model(), "")
        self._write(MODEL, MODEL_FILES)
        self.assertEqual(self.transcriber.downloaded_models(), [])  # VAD still missing
        self.assertEqual(self.transcriber.active_model(), "")
        self._write(VAD_DIR, (VAD_FILE,))
        self.assertEqual(self.transcriber.downloaded_models(), [MODEL])
        self.assertEqual(self.transcriber.active_model(), MODEL)

    def test_download_fetches_only_the_needed_files(self) -> None:
        """Fetch the VAD and the int8 model files into the cache, then report success."""
        finished = MagicMock()
        self.transcriber.download_finished.connect(finished)
        with patch("huggingface_hub.snapshot_download") as download:
            self.transcriber._download(MODEL)
        cache = self.transcriber.cache_dir
        self.assertEqual(
            download.call_args_list,
            [
                call(
                    VAD_REPO, local_dir=str(cache / VAD_DIR), allow_patterns=[VAD_FILE]
                ),
                call(
                    f"istupakov/{MODEL}-onnx",
                    local_dir=str(cache / MODEL),
                    allow_patterns=list(MODEL_FILES),
                ),
            ],
        )
        finished.assert_called_once_with(MODEL, "")

    def test_model_loads_once_offline_and_warms_up(self) -> None:
        """Load from the local folder with bounded threads and run one silent warm-up."""
        (self.transcriber.cache_dir / MODEL).mkdir()
        statuses: list[str] = []
        self.transcriber.status_changed.connect(statuses.append)
        with patch("onnx_asr.load_model") as load:
            model = self.transcriber._load_model(MODEL)
            self.assertIs(self.transcriber._load_model(MODEL), model)
        load.assert_called_once()
        self.assertEqual(
            load.call_args.args, (f"nemo-{MODEL}", self.transcriber.cache_dir / MODEL)
        )
        self.assertEqual(load.call_args.kwargs["quantization"], "int8")
        self.assertEqual(
            load.call_args.kwargs["sess_options"].intra_op_num_threads, ONNX_THREADS
        )
        model.recognize.assert_called_once()
        self.assertEqual(statuses, [f"Loading the {MODEL} model…", ""])

    def test_vad_is_shared_when_downloaded_and_absent_otherwise(self) -> None:
        """Fall back to the energy gate without the VAD, and load one session for both sources."""
        self.assertIsNone(self.transcriber._vad())
        self._write(VAD_DIR, (VAD_FILE,))
        with patch("onnxruntime.InferenceSession") as session:
            first, second = self.transcriber._vad(), self.transcriber._vad()
        session.assert_called_once()
        assert first is not None and second is not None
        self.assertIs(first.session, second.session)
        self.assertIsNot(first, second)  # Each source keeps its own recurrent state

    def _write(self, folder: str, names: tuple[str, ...]) -> None:
        """Create placeholder model files.

        Args:
            folder (str): The folder inside the cache.
            names (tuple[str, ...]): The file names to create.
        """
        (self.transcriber.cache_dir / folder).mkdir(exist_ok=True)
        for name in names:
            (self.transcriber.cache_dir / folder / name).write_text("x")


class TranscriptDeliveryTests(unittest.TestCase):
    """Send each transcript line once, and only after a committed turn."""

    def setUp(self) -> None:
        """Create a transcriber without starting audio capture."""
        self.app = application()
        self.transcriber = Transcriber(MagicMock())

    def test_pending_until_marked_sent(self) -> None:
        """Keep lines pending through failed turns and drop them after commit."""
        self.transcriber.recording = True
        self._record_utterance("them", "Reverse a linked list.")
        self._record_utterance("you", "Iteratively?")
        text, seq = self.transcriber.pending()
        self.assertEqual(text, "[Them] Reverse a linked list.\n[You] Iteratively?")
        self.assertEqual(self.transcriber.pending(), (text, seq))  # Not committed yet
        self._record_utterance("them", "Yes.")
        self.transcriber.mark_sent(seq)
        self.assertEqual(self.transcriber.pending(), ("[Them] Yes.", seq + 1))
        self.transcriber.mark_sent(seq - 1)  # A stale commit never resends lines
        self.assertEqual(self.transcriber.pending()[0], "[Them] Yes.")

    def test_speaker_echo_is_merged(self) -> None:
        """Keep one computer-audio line when the microphone hears the speakers."""
        self.transcriber.recording = True
        line = "Return the indices of two numbers that add up to the target."
        self._record_utterance("you", line)
        self._record_utterance("them", line.lower())
        self._record_utterance("you", line)
        self._record_utterance("you", "Can I assume the input is sorted?")
        self.assertEqual(
            [segment.source for segment in self.transcriber.segments], ["them", "you"]
        )

    def test_spoken_reply_is_not_transcribed_as_them(self) -> None:
        """Drop Que's own voice heard on the playback device, but never your own words."""
        self.transcriber.recording = True
        self.transcriber.note_spoken("Use a hash map so each lookup is constant time.")
        self.transcriber.note_spoken("Then sort the array once.")
        self._record_utterance("them", "use a hash map so each lookup is constant")
        self._record_utterance("you", "Use a hash map so each lookup is constant time.")
        self._record_utterance("them", "So what is the time complexity of the sort?")
        self._record_utterance("them", "Okay.")
        self.assertEqual(
            [(segment.source, segment.text) for segment in self.transcriber.segments],
            [
                ("you", "Use a hash map so each lookup is constant time."),
                ("them", "So what is the time complexity of the sort?"),
                ("them", "Okay."),
            ],
        )

    def test_old_speech_is_forgotten(self) -> None:
        """Keep only speech recent enough to come back through the playback device."""
        with patch("core.transcriber.time.monotonic", side_effect=[0.0, 100.0]):
            self.transcriber.note_spoken("First sentence spoken long ago.")
            self.transcriber.note_spoken("Second sentence.")
        self.assertEqual(
            [text for _when, text in self.transcriber.spoken], ["Second sentence."]
        )

    def test_start_without_model_asks_for_download(self) -> None:
        """Show a settings notice instead of recording or downloading on first record."""
        statuses: list[str] = []
        self.transcriber.status_changed.connect(statuses.append)
        with patch.object(self.transcriber, "downloaded_models", return_value=[]):
            self.transcriber.start()
            self.assertFalse(self.transcriber.recording)
            self.assertTrue(self.transcriber.missing_model)
            self.assertIn("Settings", statuses[-1])
            self.transcriber.downloading = "tiny.en"
            self.transcriber.start()
        self.assertIn("tiny.en model is still downloading", statuses[-1])
        self.transcriber.download_finished.emit("tiny.en", "")
        self.assertFalse(self.transcriber.missing_model)
        self.assertEqual(statuses[-1], "")

    def test_caption_explains_old_model_during_download(self) -> None:
        """Name the model in use until this recording's first line or the download ends."""
        window = QWidget()
        panel = TranscriptPanel(self.transcriber, window)
        with (
            patch.object(
                self.transcriber, "downloaded_models", return_value=["tiny.en"]
            ),
            patch.object(self.transcriber, "_transcribe_loop"),
            patch.object(self.transcriber, "_capture_sources"),
        ):
            self.transcriber.start()
            self._record_utterance("you", "From an earlier recording")
            self.transcriber.stop()
            self.transcriber.downloading = "small.en"
            self.transcriber.start()
        note = "Using tiny.en while small.en downloads…"
        self.assertEqual(panel.caption.toolTip(), note)
        self.assertIn("Transcribing with tiny.en.", panel.text.text())
        self.assertTrue(panel.toggle_button.toolTip().endswith(" · tiny.en"))
        self._record_utterance("them", "First new line")
        self.assertEqual(panel.caption.toolTip(), "Them: First new line")
        self.transcriber.stop()

    def test_clear_drops_unsent_lines(self) -> None:
        """Start a new chat with an empty transcript."""
        self.transcriber.recording = True
        self._record_utterance("you", "Hello")
        self.transcriber.clear()
        self.assertEqual(self.transcriber.pending()[0], "")
        self._record_utterance("you", "Again")
        self.assertEqual(self.transcriber.pending()[0], "[You] Again")

    def _record_utterance(self, source: str, text: str) -> None:
        """Deliver a synthetic utterance for the current recording."""
        self.transcriber.transcribed.emit(source, text, self.transcriber.stop_event)


class PromptTests(unittest.TestCase):
    """Prepend transcript context to the provider prompt only."""

    def test_prompt_wraps_transcript(self) -> None:
        """Leave the visible message unchanged and add the transcript to the prompt."""
        request = InteractionRequest(
            "Solve it", (), "m", "default", None, 0, transcript="[Them] Two sum."
        )
        self.assertEqual(request.message, "Solve it")
        self.assertIn("<transcript>\n[Them] Two sum.\n</transcript>", request.prompt)
        self.assertTrue(request.prompt.endswith("\n\nSolve it"))
        plain = InteractionRequest("Solve it", (), "m", "default", None, 0)
        self.assertEqual(plain.prompt, "Solve it")


if __name__ == "__main__":
    unittest.main()
