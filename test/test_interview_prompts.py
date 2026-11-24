"""Verify the interview shortcut sends its full prompt while the chat shows only its label."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.ai_sender import AISender
from core.hook_engine import ACTION_NAMES
from core.model_catalog import ModelOption
from core.prompts import (
    INTERVIEW_CODE,
    INTERVIEW_EVERYTHING_ELSE,
    INTERVIEW_FOLLOW_UP,
    INTERVIEW_KINDS,
    INTERVIEW_PREAMBLE,
    INTERVIEW_PROMPT,
    INTERVIEW_SYSTEM_DESIGN,
)
from core.settings import SettingsStore
from ui.window.main_window import MainWindow


class InterviewPromptTests(unittest.TestCase):
    """Check prompt wiring, the display label and typed notes."""

    def test_one_shortcut_covers_every_kind(self) -> None:
        """Bind the answer action and send every kind's format in one prompt."""
        self.assertIn("answer", ACTION_NAMES)
        self.assertTrue(INTERVIEW_PROMPT.startswith(INTERVIEW_PREAMBLE))
        for part in (
            INTERVIEW_KINDS,
            INTERVIEW_FOLLOW_UP,
            INTERVIEW_CODE,
            INTERVIEW_SYSTEM_DESIGN,
            INTERVIEW_EVERYTHING_ELSE,
        ):
            self.assertIn(part, INTERVIEW_PROMPT)
        for kind in ("Follow-up", "Coding", "System design", "Everything else"):
            self.assertIn(f"**{kind}**", INTERVIEW_KINDS)

    def test_display_label_is_never_sent(self) -> None:
        """Keep the chat label out of the prompt the provider receives."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sender = AISender(
            SettingsStore(Path(directory.name) / "config.json"), client=MagicMock()
        )
        sender.set_models((ModelOption("m", "M", ("default",)),))
        request = sender.prepare_request("Full prompt", display="Code")
        self.assertEqual(request.prompt, "Full prompt")
        self.assertEqual(request.display, "Code")

    def test_typed_note_joins_prompt_and_label(self) -> None:
        """Send typed text as a note and clear it only when a model can send it."""
        for model, cleared in (("m", True), ("", False)):
            with self.subTest(model=model):
                window = MagicMock()
                window.ai_sender.model = model
                window.input_field.input_field.toPlainText.return_value = " Why us? "
                MainWindow.send_prompt(window, "Answer", "Prompt")
                window.send_message.assert_called_once_with(
                    "Prompt\n\nMy note: Why us?", "Answer: Why us?"
                )
                self.assertEqual(window.input_field.input_field.clear.called, cleared)

    def test_no_note_sends_label_alone(self) -> None:
        """Leave the prompt and label unchanged when nothing is typed."""
        window = MagicMock()
        window.input_field.input_field.toPlainText.return_value = "  "
        MainWindow.send_prompt(window, "Code", "Prompt")
        window.send_message.assert_called_once_with("Prompt", "Code")
        window.input_field.input_field.clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
