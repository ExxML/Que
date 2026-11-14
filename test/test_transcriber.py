"""Verify utterance segmentation, transcript delivery tracking, and prompt composition."""

import unittest
from unittest.mock import MagicMock

import numpy as np
from PyQt6.QtWidgets import QApplication

from core.ai_sender import InteractionRequest
from core.transcriber import (
    CHUNK_SECONDS,
    SAMPLE_RATE,
    Transcriber,
    _Segmenter,
)


CHUNK = int(SAMPLE_RATE * CHUNK_SECONDS)


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


class TranscriptDeliveryTests(unittest.TestCase):
    """Send each transcript line once, and only after a committed turn."""

    def setUp(self) -> None:
        """Create a transcriber without starting audio capture."""
        self.app = QApplication.instance() or QApplication([])
        self.transcriber = Transcriber(MagicMock())

    def test_pending_until_marked_sent(self) -> None:
        """Keep lines pending through failed turns and drop them after commit."""
        self.transcriber._on_transcribed("them", "Reverse a linked list.")
        self.transcriber._on_transcribed("you", "Iteratively?")
        text, seq = self.transcriber.pending()
        self.assertEqual(text, "[Them] Reverse a linked list.\n[You] Iteratively?")
        self.assertEqual(self.transcriber.pending(), (text, seq))  # Not committed yet
        self.transcriber._on_transcribed("them", "Yes.")
        self.transcriber.mark_sent(seq)
        self.assertEqual(self.transcriber.pending(), ("[Them] Yes.", seq + 1))
        self.transcriber.mark_sent(seq - 1)  # A stale commit never resends lines
        self.assertEqual(self.transcriber.pending()[0], "[Them] Yes.")

    def test_speaker_echo_is_merged(self) -> None:
        """Keep one computer-audio line when the microphone hears the speakers."""
        line = "Return the indices of two numbers that add up to the target."
        self.transcriber._on_transcribed("you", line)
        self.transcriber._on_transcribed("them", line.lower())
        self.transcriber._on_transcribed("you", line)
        self.transcriber._on_transcribed("you", "Can I assume the input is sorted?")
        self.assertEqual(
            [segment.source for segment in self.transcriber.segments], ["them", "you"]
        )

    def test_clear_drops_unsent_lines(self) -> None:
        """Start a new chat with an empty transcript."""
        self.transcriber._on_transcribed("you", "Hello")
        self.transcriber.clear()
        self.assertEqual(self.transcriber.pending()[0], "")
        self.transcriber._on_transcribed("you", "Again")
        self.assertEqual(self.transcriber.pending()[0], "[You] Again")


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
