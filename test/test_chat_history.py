"""Verify conversation persistence and resuming saved provider context."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.ai_sender import AISender
from core.chat_history import ChatHistory, Conversation
from core.model_catalog import ModelOption
from ui.window.history_sidebar import format_age


class ChatHistoryTests(unittest.TestCase):
    """Exercise the JSON history store and saved-context restoration."""

    def setUp(self) -> None:
        """Create an isolated history file location."""
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "chat_history.json"

    def tearDown(self) -> None:
        """Remove the temporary directory."""
        self.directory.cleanup()

    def test_round_trip_orders_newest_first_and_deletes(self) -> None:
        """Reload saved conversations in recency order and persist deletion."""
        history = ChatHistory(self.path)
        old = Conversation("gemini", "Old", updated=1.0, interaction_id="g1")
        new = Conversation(
            "claude",
            "New",
            updated=2.0,
            messages=[{"role": "user", "text": "hi"}],
            interaction_id="c1",
            session_id="s1",
        )
        history.save(old)
        history.save(new)

        reloaded = ChatHistory(self.path)
        self.assertEqual(reloaded.conversations(), [new, old])

        reloaded.delete(new.conversation_id)
        self.assertEqual(ChatHistory(self.path).conversations(), [old])

    def test_corrupt_file_is_backed_up_not_overwritten(self) -> None:
        """Move an unreadable history aside so a later save cannot destroy it."""
        self.path.write_text("{not json", encoding="utf-8")
        history = ChatHistory(self.path)
        self.assertEqual(history.conversations(), [])
        self.assertEqual(
            self.path.with_suffix(".bak").read_text(encoding="utf-8"), "{not json"
        )

    def test_restore_chat_resumes_saved_context(self) -> None:
        """Send the next turn with the saved interaction and session identifiers."""
        settings = MagicMock()
        settings.gemini_key.return_value = ""
        settings.provider.return_value = "claude"
        sender = AISender(settings, MagicMock())
        sender.set_models((ModelOption("opus", "Opus", ("default",)),))
        sender.restore_chat("assistant-uuid", "session-uuid")
        request = sender.prepare_request("continue")
        self.assertEqual(request.previous_interaction_id, "assistant-uuid")
        self.assertEqual(request.session_id, "session-uuid")

    def test_format_age(self) -> None:
        """Describe recent and older timestamps compactly."""
        self.assertEqual(format_age(100, 130), "Just now")
        self.assertEqual(format_age(0, 300), "5m ago")
        self.assertEqual(format_age(0, 7200), "2h ago")
        self.assertEqual(format_age(0, 3 * 86400), "3d ago")


if __name__ == "__main__":
    unittest.main()
