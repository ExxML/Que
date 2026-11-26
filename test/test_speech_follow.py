"""Verify spoken-word alignment, formatting, and chat follow behavior."""

import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, cast
from unittest.mock import MagicMock, patch

import numpy as np
import onnx
from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QTextCursor, QTextDocument, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractSlider, QVBoxLayout, QWidget
from qt_application import application

from core.speech_timing import (
    VOICE_RATE,
    ReadingPosition,
    _add_duration_output,
    load_voice,
    synthesize,
)
from ui.chat.chat_area import ChatArea
from ui.chat.chat_bubble import ChatBubble
from ui.chat.source_text import RenderedSources, SourceText
from ui.chat.speech_text import speech_units
from ui.theme import SPOKEN_WORD_COLOR, qcolor

wait = cast(Callable[[int], None], QTest.qWait)


class SpeechFollowTests(unittest.TestCase):
    """Exercise the real chat document and scrollbar without an audio device."""

    def setUp(self) -> None:
        """Show a chat with enough wrapped lines to scroll in both directions."""
        self.app = application()
        self.window = QWidget()
        self.addCleanup(self.window.close)
        layout = QVBoxLayout(self.window)
        self.chat = ChatArea(self.window)
        layout.addWidget(self.chat)
        self.window.resize(360, 220)
        self.window.show()
        self.app.processEvents()

    def load_reply(self, markdown: str) -> ChatBubble:
        """Load a reply and return the displayed assistant bubble."""
        self.chat.load_messages([(markdown, False, "", ())])
        wait(80)
        bubble = self.chat.latest_reply()
        assert bubble is not None
        return bubble

    def test_tint_follows_markdown_sources_without_changing_selection_or_layout(
        self,
    ) -> None:
        """Tint the second occurrence and restore rich-text formatting when it moves."""
        markdown = (
            "# Title\n\n💡 Echo **Echo**, [link label](https://example.com), "
            "`getFileSystem()` and $x^2$.\n\n"
            "| First | Second |\n| --- | --- |\n| Echo one | Echo two |\n\n"
            "$$\\frac{a}{b}$$\r\n\r\n- [ ] Check me.\r\n\r\n>\tIndented quote."
        )
        bubble = self.load_reply(markdown)
        label = bubble.message_label
        document = label.findChild(QTextDocument)
        assert document is not None
        first = document.find("Echo")
        second = document.find("Echo", first)
        label.setSelection(first.selectionStart(), 4)
        original = second.charFormat()
        height = label.heightForWidth(label.width())
        self.chat.set_reading_state(bubble, "start", None)
        expected = ["Echo", "Echo", "link", "label", "getFileSystem()", "and", "x2"]
        rendered = set()
        units = speech_units(markdown, True)
        for unit in units:
            self.assertEqual(
                speech_units(markdown, True, unit.start, first_only=True), [unit]
            )
            for word in unit.words:
                position = ReadingPosition((unit.start, unit.end), word.source)
                self.chat.set_reading_state(bubble, "position", position)
                cursor = QTextCursor(document)
                current = []
                for start, end in label.tint_ranges:
                    cursor.setPosition(start)
                    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                    current.append(cursor.selectedText())
                    self.assertEqual(
                        cursor.charFormat().foreground().color(),
                        qcolor(SPOKEN_WORD_COLOR),
                    )
                self.assertTrue(current, word.text)
                rendered.update(current)
                if word.source[0] == markdown.index("**Echo") + 2:
                    self.assertEqual(
                        label.tint_ranges,
                        [(second.selectionStart(), second.selectionEnd())],
                    )
                    self.assertEqual(
                        second.charFormat().fontWeight(), original.fontWeight()
                    )
        self.assertTrue(set(expected + ["a", "b", "one", "two"]).issubset(rendered))
        self.assertTrue({"Check", "Indented"}.issubset(rendered))
        self.assertEqual(second.charFormat(), original)
        self.assertEqual(label.selectedText(), "Echo")
        self.assertEqual(label.heightForWidth(label.width()), height)
        self.chat.set_reading_state(bubble, "finish", None)
        self.assertFalse(label.tint_ranges)

    def test_center_one_line_at_a_time_and_hold_after_manual_scroll(self) -> None:
        """Reuse one glide per line and preserve manual navigation until speech resumes."""
        markdown = "\n".join(f"Line {index} says hello" for index in range(80))
        bubble = self.load_reply(markdown)
        chat = self.chat
        middle = speech_units(markdown, True)[30]
        following = speech_units(markdown, True)[31]
        first = ReadingPosition((middle.start, middle.end), middle.words[0].source)
        same_line = ReadingPosition((middle.start, middle.end), middle.words[-1].source)
        next_line = ReadingPosition(
            (following.start, following.end), following.words[0].source
        )
        chat.set_reading_state(bubble, "start", None)
        chat.set_reading_state(bubble, "position", first)
        center = chat.scrollbar.value()
        line = bubble.speech_line(first.word)
        viewport = chat.viewport()
        assert line is not None and viewport is not None
        self.assertAlmostEqual(
            line.center().y() + bubble.y() - center, viewport.height() / 2, delta=1
        )
        chat.set_reading_state(bubble, "position", same_line)
        self.assertEqual(chat.scrollbar.value(), center)
        chat.set_reading_state(
            bubble,
            "position",
            ReadingPosition(first.unit, (len(markdown), len(markdown))),
        )
        self.assertEqual(chat.scrollbar.value(), center)
        self.assertFalse(bubble.message_label.tint_ranges)
        chat.set_reading_state(bubble, "position", next_line)
        self.assertEqual(chat.scroll_animation.duration(), 150)
        wait(170)
        self.assertGreater(chat.scrollbar.value(), center)

        for action in ("shortcut", "wheel", "track", "drag"):
            with self.subTest(action=action):
                if action == "shortcut":
                    chat.shortcut_scroll(-170)
                elif action == "wheel":
                    chat.wheelEvent(
                        QWheelEvent(
                            QPointF(),
                            QPointF(),
                            QPoint(),
                            QPoint(0, 240),
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase,
                            False,
                        )
                    )
                elif action == "track":
                    chat.scrollbar.triggerAction(
                        QAbstractSlider.SliderAction.SliderPageStepSub
                    )
                else:
                    chat.scrollbar.setSliderDown(True)
                    chat.scrollbar.setSliderPosition(chat.scrollbar.value() - 150)
                    chat.scrollbar.setSliderDown(False)
                wait(170)
                held = chat.scrollbar.value()
                chat.set_reading_state(bubble, "position", first)
                wait(170)
                self.assertEqual(chat.scrollbar.value(), held)
                self.assertTrue(bubble.message_label.tint_ranges)
                chat.set_reading_state(bubble, "pause", first)
                chat.set_reading_state(bubble, "resume", first)
                wait(170)
                self.assertEqual(chat.scrollbar.value(), center)

    def test_autoscroll_and_highlight_toggle_during_a_reading(self) -> None:
        """Apply each toggle on its own to the current word without waiting for the next."""
        markdown = "\n".join(f"Line {index} says hello" for index in range(80))
        bubble = self.load_reply(markdown)
        chat = self.chat
        unit = speech_units(markdown, True)[30]
        position = ReadingPosition((unit.start, unit.end), unit.words[0].source)
        start = chat.scrollbar.value()
        chat.set_speech_autoscroll(False)
        chat.set_reading_state(bubble, "start", None)
        chat.set_reading_state(bubble, "position", position)
        wait(170)
        self.assertEqual(chat.scrollbar.value(), start)
        self.assertTrue(bubble.message_label.tint_ranges)

        chat.set_speech_highlight(False)
        wait(20)
        self.assertFalse(bubble.message_label.tint_ranges)

        chat.set_speech_autoscroll(True)
        wait(170)
        self.assertNotEqual(chat.scrollbar.value(), start)
        self.assertFalse(bubble.message_label.tint_ranges)

        chat.set_speech_highlight(True)
        wait(20)
        self.assertTrue(bubble.message_label.tint_ranges)

    def test_skipped_code_and_scroll_edges(self) -> None:
        """Keep both ends readable and go directly to the line after a long skipped block."""
        markdown = "Start.\n\n```\n" + "skip this line\n" * 80 + "```\n\nEnd."
        bubble = self.load_reply(markdown)
        chat = self.chat
        viewport = chat.viewport()
        assert viewport is not None
        chat.set_reading_state(bubble, "start", None)
        units = speech_units(markdown, True)
        self.assertEqual([unit.text for unit in units], ["Start.", "End."])
        self.assertGreater(chat.scrollbar.maximum(), chat.scrollbar.minimum())
        for unit, limit in zip(
            units, (chat.scrollbar.minimum(), chat.scrollbar.maximum()), strict=True
        ):
            span = unit.words[0].source
            chat.set_reading_state(
                bubble, "position", ReadingPosition((unit.start, unit.end), span)
            )
            self.assertEqual(chat.scrollbar.value(), limit)
            line = bubble.speech_line(span)
            assert line is not None
            self.assertGreaterEqual(bubble.y() + line.top() - limit, 0)
            self.assertLessEqual(bubble.y() + line.bottom() - limit, viewport.height())
        viewport.setFixedHeight(0)
        chat._center_reading_line(QRectF(0, 0, 1, 1))

    def test_stream_redraw_hidden_chat_and_replacement(self) -> None:
        """Reapply a word once its streamed text is visible and discard it on replacement."""
        chat = self.chat
        chat.start_assistant_stream()
        bubble = chat.latest_reply()
        assert bubble is not None
        chat.set_reading_state(bubble, "start", None)
        text = "Scrolled " * 80 + "now. "
        end = text.index("now")
        position = ReadingPosition((0, len(text) - 1), (end, end + 3))
        chat.set_reading_state(bubble, "position", position)
        self.assertFalse(bubble.message_label.tint_ranges)
        self.window.hide()
        chat.append_to_stream(text)
        self.app.processEvents()
        self.assertFalse(bubble.message_label.tint_ranges)
        self.window.show()
        self.app.processEvents()
        self.assertTrue(bubble.message_label.tint_ranges)
        self.assertGreater(chat.scrollbar.value(), 0)

        previous_map = bubble.message_label.source_positions
        chat.append_to_stream("Next words. ")
        wait(80)
        self.assertIs(bubble.message_label.source_positions, previous_map)
        self.assertTrue(bubble.message_label.tint_ranges)

        self.window.resize(470, 220)
        chat.set_stream_status("Waiting.")
        wait(170)
        self.assertTrue(bubble.message_label.tint_ranges)
        bubble.message_label.setText("Text with no source mapping.")
        self.assertIsNone(bubble.speech_line(position.word))
        chat.show_stream_error("Connection interrupted.")
        self.assertFalse(bubble.message_label.tint_ranges)
        chat.set_reading_state(bubble, "position", position)
        self.assertIsNone(chat.reading_bubble)


class _Phoneme(NamedTuple):
    """Provide the timing fields produced by Kokoro."""

    phoneme: str
    start: float
    end: float


class SpeechTimingTests(unittest.TestCase):
    """Validate timings without a production model or audio endpoint."""

    def test_duration_output_reuses_the_cached_graph(self) -> None:
        """Add the duration output while keeping the cached file byte-for-byte."""
        helper = onnx.helper
        count = helper.make_tensor_value_info("frames", onnx.TensorProto.INT64, [None])
        audio = helper.make_tensor_value_info("audio", onnx.TensorProto.FLOAT, [None])
        graph = helper.make_graph(
            [
                helper.make_node(
                    "Cast", ["frames"], ["audio"], to=onnx.TensorProto.FLOAT
                ),
                helper.make_node("Identity", ["frames"], ["/encoder/Gather_output_0"]),
            ],
            "fixture",
            [count],
            [audio],
        )
        model = helper.make_model(
            graph, ir_version=8, opset_imports=[helper.make_opsetid("", 13)]
        ).SerializeToString()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cached.onnx"
            path.write_bytes(model)
            augmented = _add_duration_output(path)
            self.assertEqual(path.read_bytes(), model)
            patched_model = onnx.load_model_from_string(augmented)
            onnx.checker.check_model(patched_model)
            self.assertEqual(
                [value.name for value in patched_model.graph.output],
                ["audio", "duration"],
            )
            with patch(
                "kokoro_onnx.Kokoro.from_session",
                side_effect=lambda session, _voices: session,
            ):
                voice = load_voice(path, path, 1)
                self.assertEqual(
                    [output.name for output in voice.get_outputs()],
                    ["audio", "duration"],
                )
            path.write_bytes(augmented)
            self.assertEqual(_add_duration_output(path), augmented)
            del graph.node[-1]
            path.write_bytes(helper.make_model(graph).SerializeToString())
            with self.assertRaises(ValueError):
                _add_duration_output(path)

    def test_timed_synthesis_groups_expansions_and_scales_with_the_audio(self) -> None:
        """Use the actual synthesized phoneme times and hold one source for an expanded token."""
        unit = speech_units("One 42 then.", True)[0]
        voice = MagicMock()
        voice.has_timings = True
        voice.tokenizer.known.side_effect = str
        voice.tokenizer.phonemize.side_effect = lambda word, _lang: {
            "One": "w",
            "42": "f t",
            "then.": "d.",
        }[word]
        text = "w f t d."
        voice._prepare.return_value = (None, text, [(text, 0)])
        for speed in (0.5, 1.0, 2.0):
            duration = 0.1 / speed
            audio = np.ones(round(len(text) * duration * VOICE_RATE), np.float32)
            voice._create_batches.return_value = (
                audio,
                [
                    _Phoneme(char, index * duration, (index + 1) * duration)
                    for index, char in enumerate(text)
                ],
            )
            samples, cues = synthesize(voice, unit, "af_heart", speed)
            np.testing.assert_array_equal(samples, audio)
            active = [
                (point, position.word)
                for point, position in cues
                if position is not None
            ]
            self.assertEqual(
                active,
                [
                    (round(index * duration * VOICE_RATE), word.source)
                    for index, word in zip((0, 2, 6), unit.words, strict=True)
                ],
            )
        voice.create.assert_not_called()

    def test_alignment_falls_back_without_losing_the_audio(self) -> None:
        """Keep timing for usable groups and retain audio if alignment fails."""
        voice = MagicMock()
        voice.has_timings = True
        unit = speech_units("42.", True)[0]
        speech = (_Phoneme("x", 0, 0.1),)
        audio = np.ones(2400, np.float32)
        voice._prepare.return_value = (None, "x", [("x", 0)])
        voice._create_batches.return_value = (audio, speech)
        voice.tokenizer.known.return_value = "x"
        voice.tokenizer.phonemize.return_value = "f t"
        fallback = ((0, ReadingPosition((0, 3), None)), (2400, None))
        samples, cues = synthesize(voice, unit, "af_heart", 1.0)
        self.assertEqual(cues, ((0, ReadingPosition((0, 3), (0, 2))), (2400, None)))
        np.testing.assert_array_equal(samples, audio)
        voice.tokenizer.phonemize.return_value = ""
        self.assertEqual(synthesize(voice, unit, "af_heart", 1.0)[1], fallback)
        voice.tokenizer.phonemize.return_value = "x"
        voice._create_batches.return_value = (audio[:0], speech)
        self.assertEqual(synthesize(voice, unit, "af_heart", 1.0)[1], ())
        voice._create_batches.return_value = (audio, speech)
        voice.tokenizer.known.side_effect = ValueError("Invalid timing.")
        self.assertEqual(synthesize(voice, unit, "af_heart", 1.0)[1], fallback)

    def test_generated_text_has_no_source_anchor(self) -> None:
        """Leave generated text out of the spoken-word mapping."""
        sources = RenderedSources()
        markup = "<span>Generated text.</span>"
        self.assertEqual(sources.mark(SourceText(markup)), markup)
        self.assertFalse(sources.runs)
