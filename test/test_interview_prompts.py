"""Verify the interview shortcuts send full prompts while the chat shows only their labels."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.ai_sender import AISender
from core.hook_engine import ACTION_NAMES
from core.interview_prompts import PREAMBLE, PROMPTS
from core.model_catalog import ModelOption
from core.settings import SettingsStore
from ui.window.main_window import MainWindow


class InterviewPromptTests(unittest.TestCase):
    """Check prompt wiring, the display label and typed notes."""

    def test_every_prompt_is_a_shortcut_action(self) -> None:
        """Bind each prompt to a hotkey action and share the interview preamble."""
        self.assertTrue(set(PROMPTS) <= set(ACTION_NAMES))
        for label, prompt in PROMPTS.values():
            self.assertTrue(label)
            self.assertTrue(prompt.startswith(PREAMBLE))

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
