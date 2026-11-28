"""Tests for the main window's visibility, quitting, presets, history and model paths.

The window re-raises itself when another window covers it, masks the API key whenever it
hides, and lets clicks through on request; the rest keeps chats, presets and models right.
Every test uses a temporary settings store and chat history, so no real data is touched.
"""

import ctypes
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QScrollArea
from qt_application import application

from core.ai_sender import AISender, InteractionRequest
from core.chat_history import ChatHistory, Conversation
from core.model_catalog import ModelOption
from core.provider_catalog import ProviderResult
from core.screenshot_manager import ScreenshotManager
from core.settings import SettingsStore
from core.speaker import Speaker
from ui.window.history_sidebar import _ConversationRow
from ui.window.main_window import MainWindow

FLASH = ModelOption("flash", "Flash", ("low", "high"))


class FakeUser32:
    """Answer the z-order queries behind the re-raise check."""

    def __init__(self, above: list[int], at_point: int) -> None:
        self.above = list(above)
        self.at_point = at_point

    def GetAncestor(self, hwnd: Any, _flag: int) -> int:
        """Return the window as its own root.

        Args:
            hwnd (Any): The window.

        Returns:
            int: Its handle.
        """
        return int(hwnd.value if hasattr(hwnd, "value") else hwnd)

    def GetWindow(self, _hwnd: int, _command: int) -> int:
        """Walk up the z-order through the windows above.

        Returns:
            int: The next window above, or zero at the top.
        """
        return self.above.pop(0) if self.above else 0

    def WindowFromPoint(self, _point: Any) -> int:
        """Return the window under every sampled corner.

        Returns:
            int: The handle.
        """
        return self.at_point


class WindowTestCase(unittest.TestCase):
    """Build a main window over temporary storage, with model discovery disabled."""

    def setUp(self) -> None:
        """Create the window."""
        self.app = application()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = SettingsStore(root / "config.json")
        self.history = ChatHistory(root / "history.json")
        self.screenshots = ScreenshotManager()
        self.screenshots.screenshots_dir = root / "screenshots"
        self.screenshots.screenshots_dir.mkdir()
        with (
            patch("core.provider_catalog.ProviderCatalog.refresh", return_value=1),
            patch("ui.window.main_window.ChatHistory", return_value=self.history),
        ):
            self.window = MainWindow(
                AISender(self.store),
                self.screenshots,
                speaker=Speaker(self.store, root / "voice"),
            )
        self.addCleanup(self.window.close)
        self.window.visibility_timer.stop()
        self.sender = self.window.ai_sender
        self.warnings: list[str] = []
        banner = patch.object(
            self.window.status_banner,
            "show_warning",
            side_effect=lambda text, **_: self.warnings.append(text),
        )
        banner.start()
        self.addCleanup(banner.stop)

    def load_models(self) -> None:
        """Install one model as if discovery had found it."""
        self.window._apply_models(ProviderResult(self.sender.provider, (FLASH,), ""))


class VisibilityTests(WindowTestCase):
    """Show, hide, raise and quit the overlay."""

    def test_toggle_shows_and_hides(self) -> None:
        """Show and raise a hidden window, and hide a shown one."""
        self.window.hide()  # The window shows itself on creation
        self.window.toggle_window_visibility()
        self.assertTrue(self.window.isVisible())
        self.window.toggle_window_visibility()
        self.assertFalse(self.window.isVisible())

    def test_hiding_masks_the_api_key(self) -> None:
        """Mask a revealed API key whenever the overlay hides."""
        self.window.show()
        page = self.window.settings_page
        page.show_key.setChecked(True)
        page.key_input.setEchoMode(page.key_input.EchoMode.Normal)
        self.window.hide()
        self.assertEqual(page.key_input.echoMode(), page.key_input.EchoMode.Password)
        self.assertFalse(page.show_key.isChecked())

    def test_click_through_switches_routing_off(self) -> None:
        """Stop routing clicks to Que when click-through is turned on."""
        routing: list[bool] = []
        self.window.mouse_routing_changed.connect(routing.append)
        self.window.set_click_through(True)
        self.window.set_click_through(False)
        self.assertEqual(routing, [False, True])

    def test_covered_window_is_raised(self) -> None:
        """Raise the overlay when a window above it covers a corner, and only then."""
        self.window.show()
        hwnd = int(self.window.winId())
        with patch.object(self.window, "raise_") as raise_:
            with patch.object(ctypes.windll, "user32", FakeUser32([77], 77)):
                self.window._ensure_window_visible()
            raise_.assert_called_once()
            with patch.object(ctypes.windll, "user32", FakeUser32([77], hwnd)):
                self.window._ensure_window_visible()
            raise_.assert_called_once()

    def test_failed_check_is_reported_once_then_cleared(self) -> None:
        """Warn once when the check fails, and clear the warning once it works again."""
        with patch.object(ctypes.windll, "user32", None):
            self.assertIsNone(self.window._is_topmost_window())
            self.assertIsNone(self.window._is_topmost_window())
        self.assertEqual(len(self.warnings), 1)
        with (
            patch.object(ctypes.windll, "user32", FakeUser32([], 0)),
            patch.object(self.window.status_banner, "clear_warning") as clear,
        ):
            self.assertTrue(self.window._is_topmost_window())
        clear.assert_called_once_with(self.warnings[0])
        self.assertEqual(self.window.topmost_error, "")

    def test_quit_is_requested_then_finished(self) -> None:
        """Request a quit, then stop work, clear the chat and quit the application."""
        requests: list[bool] = []
        self.window.quit_requested.connect(lambda: requests.append(True))
        self.window.quit_app()
        self.assertEqual(requests, [True])
        with (
            patch.object(self.window.worker, "stop") as stop,
            patch.object(self.screenshots, "clear_screenshots") as clear,
            patch.object(QApplication, "quit") as quit_,
        ):
            self.window.finish_quit()
        stop.assert_called_once()
        clear.assert_called_once()
        quit_.assert_called_once()

    def test_clicking_the_chat_focuses_the_input(self) -> None:
        """Focus the message input when the chat page is clicked."""
        self.window._close_settings()
        self.window.show()
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(5, 5),
            QPointF(5, 5),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        with patch.object(self.window.input_field.input_field, "setFocus") as focus:
            self.window.mousePressEvent(event)
        focus.assert_called_once()

    def test_scroll_shortcut_scrolls_the_open_page(self) -> None:
        """Scroll the chat on the chat page and the open tab on the settings page."""
        self.window._close_settings()
        with patch.object(self.window.chat_area, "shortcut_scroll") as chat_scroll:
            self.window.shortcut_scroll(100)
            chat_scroll.assert_called_once_with(100)
            self.window.show_settings()
            self.window.show()
            self.window.resize(self.window.width(), 300)
            self.app.processEvents()
            tab = self.window.settings_page.tab_pages.currentWidget()
            assert isinstance(tab, QScrollArea)
            scrollbar = tab.verticalScrollBar()
            assert scrollbar is not None
            scrollbar.setValue(0)
            self.window.shortcut_scroll(100)
            self.assertEqual(scrollbar.value(), min(100, scrollbar.maximum()))
            self.assertGreater(scrollbar.value(), 0)
            self.window.shortcut_scroll(-100)
            self.assertEqual(scrollbar.value(), 0)
            chat_scroll.assert_called_once()

    def test_input_growth_grows_the_window(self) -> None:
        """Grow the window downward with the input field."""
        height = self.window.height()
        self.window._on_input_height_changed(20)
        self.assertEqual(self.window.height(), height + 20)


class ModelAndPresetTests(WindowTestCase):
    """Keep the chat's model selection and preset shortcuts honest."""

    def test_no_model_means_no_send(self) -> None:
        """Tell the user to connect a provider instead of sending without a model."""
        with patch.object(self.window.chat_area, "show_stream_error") as error:
            self.window.send_message("hello")
        error.assert_called_once()

    def test_preset_problems_are_explained(self) -> None:
        """Explain an empty, not-yet-loaded or unavailable preset."""
        self.window.apply_preset(1)
        self.store.save_presets("gemini", [("flash", "high")] + [("", "")] * 9)
        self.window.apply_preset(1)
        self.load_models()
        self.store.save_presets("gemini", [("gone", "high")] + [("", "")] * 9)
        self.window.apply_preset(1)
        self.assertEqual(len(self.warnings), 3)
        self.assertIn("no Gemini preset", self.warnings[0])
        self.assertIn("until Gemini models load", self.warnings[1])
        self.assertIn("no longer available", self.warnings[2])

    def test_preset_switches_the_model(self) -> None:
        """Switch to a valid preset without a warning."""
        self.load_models()
        self.store.save_presets("gemini", [("flash", "low")] + [("", "")] * 9)
        self.window.apply_preset(1)
        self.assertEqual(
            (self.sender.model, self.sender.effort_level), ("flash", "low")
        )
        self.assertEqual(self.warnings, [])

    def test_only_the_latest_discovery_applies(self) -> None:
        """Ignore stale or other-provider discovery results and errors."""
        self.window.catalog_revision = 5
        with patch.object(self.window, "_apply_models") as apply:
            self.window._on_models_loaded(4, ProviderResult("gemini", (FLASH,), ""))
            self.window._on_models_loaded(5, ProviderResult("claude", (FLASH,), ""))
            apply.assert_not_called()
            self.window._on_models_loaded(
                5, ProviderResult(self.sender.provider, (FLASH,), "")
            )
            apply.assert_called_once()
        with patch.object(
            self.window.input_field.input_settings, "show_model_error"
        ) as error:
            self.window._on_models_failed(4, "old")
            self.window._on_models_failed(5, "new")
        error.assert_called_once_with("new")

    def test_saving_another_provider_reconfigures(self) -> None:
        """Switch provider when settings save a different one."""
        with (
            patch.object(self.sender, "configure") as configure,
            patch.object(self.window, "_apply_models"),
        ):
            self.window._on_settings_saved(ProviderResult("claude", (FLASH,), ""))
        configure.assert_called_once_with("claude", self.store.gemini_key())

    def test_claude_directory_choice(self) -> None:
        """Use a chosen directory for Claude, and ignore a cancelled choice."""
        with patch("ui.window.main_window.choose_directory", return_value=""):
            self.window._choose_claude_directory()
        self.assertIsNone(self.sender.claude_directory)
        with patch(
            "ui.window.main_window.choose_directory", return_value=str(Path.home())
        ):
            self.window._choose_claude_directory()
        self.assertEqual(self.sender.claude_directory, Path.home())


class HistoryTests(WindowTestCase):
    """Save, reopen and delete conversations."""

    def commit(self, message: str = "Question", transcript: str = "") -> Conversation:
        """Commit one turn.

        Args:
            message (str): The user's message.
            transcript (str): Any audio transcript sent with it.

        Returns:
            Conversation: The saved conversation.
        """
        request = InteractionRequest(
            message,
            (),
            "flash",
            "low",
            None,
            0,
            provider=self.sender.provider,
            transcript=transcript,
        )
        self.window._on_turn_committed(request, "interaction-1", "Answer")
        conversation = self.window.conversations[self.sender.provider]
        assert conversation is not None
        return conversation

    def test_turns_are_saved_with_their_transcript(self) -> None:
        """Save each turn, its transcript and the reply, and refresh an open sidebar."""
        self.window.history_sidebar.opened = True
        with patch.object(self.window, "_refresh_history") as refresh:
            conversation = self.commit(transcript="[Them] hello")
        refresh.assert_called_once()
        self.assertEqual(conversation.messages[0]["transcript"], "[Them] hello")
        self.assertEqual(self.history.get(conversation.conversation_id), conversation)

    def test_save_failures_do_not_lose_the_chat(self) -> None:
        """Keep the chat open when the history file cannot be written or deleted."""
        with patch.object(self.history, "save", side_effect=OSError("disk full")):
            conversation = self.commit()
        self.assertEqual(len(conversation.messages), 2)
        with patch.object(self.history, "delete", side_effect=OSError("locked")):
            self.window._delete_conversation("missing")

    def test_reopening_a_conversation(self) -> None:
        """Load a saved conversation, switching provider if needed, and skip reopening it."""
        saved = Conversation(
            "claude", "Old chat", messages=[{"role": "user", "text": "Hi"}]
        )
        self.history.save(saved)
        with (
            patch.object(self.sender, "configure") as configure,
            patch.object(self.window, "_refresh_models"),
            patch.object(self.window.chat_area, "load_messages") as load,
        ):
            self.window._open_conversation(saved.conversation_id)
            self.window._open_conversation(
                saved.conversation_id
            )  # Already open: ignored
            self.window._open_conversation("missing")
        configure.assert_called_once_with("claude", self.sender.api_key)
        load.assert_called_once()
        self.assertEqual(self.window.conversations["claude"], saved)

    def test_deleting_the_open_conversation_starts_a_new_chat(self) -> None:
        """Clear the chat when the conversation being deleted is open."""
        conversation = self.commit()
        self.load_models()
        self.window._delete_conversation(conversation.conversation_id)
        self.assertIsNone(self.window.conversations[self.sender.provider])
        self.assertIsNone(self.history.get(conversation.conversation_id))

    def test_sidebar_toggle_and_new_chat(self) -> None:
        """Open and close the history sidebar, and close it for a new chat."""
        self.window.toggle_history()
        self.assertTrue(self.window.history_sidebar.opened)
        self.window._on_history_closed()
        self.window._new_chat()
        self.assertFalse(self.window.history_sidebar.opened)

    def history_rows(self) -> list[_ConversationRow]:
        """Return the history drawer's row widgets in list order.

        Returns:
            list[_ConversationRow]: The row shown for each conversation.
        """
        conversation_list = self.window.history_sidebar.conversation_list
        rows = []
        for index in range(conversation_list.count()):
            row = conversation_list.itemWidget(conversation_list.item(index))
            assert isinstance(row, _ConversationRow)
            rows.append(row)
        return rows

    def test_drawer_refills_its_rows_instead_of_rebuilding_them(self) -> None:
        """Reuse row widgets across reorders and deletions while showing the new list."""
        sidebar = self.window.history_sidebar
        old = Conversation("gemini", "Old chat", updated=100.0)
        new = Conversation("claude", "New chat", updated=200.0)
        sidebar.set_conversations([new, old], new.conversation_id)
        rows = self.history_rows()
        rows[1].request_delete()  # Armed rows are disarmed when shown again
        sidebar.set_conversations([old, new], old.conversation_id)
        self.assertEqual(self.history_rows(), rows)
        self.assertEqual(rows[0].conversation_id, old.conversation_id)
        self.assertEqual(rows[0].title.text(), "Old chat")
        self.assertFalse(rows[1].armed)
        self.assertEqual(sidebar.conversation_list.currentRow(), 0)
        deleted: list[str] = []
        sidebar.conversation_deleted.connect(deleted.append)
        sidebar.set_conversations([new], "")
        self.assertEqual(sidebar.conversation_list.count(), 1)
        self.assertEqual(sidebar.conversation_list.currentRow(), -1)
        rows[0].request_delete()
        rows[0].request_delete()
        self.assertEqual(deleted, [new.conversation_id])

    def test_closed_drawer_hides_once_its_animation_ends(self) -> None:
        """Hide the drawer when the closing animation ends, and ignore closing it twice."""
        sidebar = self.window.history_sidebar
        closes: list[bool] = []
        sidebar.closed.connect(lambda: closes.append(True))
        sidebar.open_sidebar()
        sidebar._on_animation_finished()  # Opening: stays shown
        self.assertTrue(sidebar.isVisible())
        sidebar.close_sidebar()
        sidebar.close_sidebar()  # Already closed: no second close
        sidebar._on_animation_finished()
        self.assertFalse(sidebar.isVisible())
        self.assertEqual(closes, [True])

    def test_transcript_scrolls_to_its_newest_line(self) -> None:
        """Scroll the transcript to the end once layout settles."""
        panel = self.window.transcript_panel
        scrollbar = panel.body.verticalScrollBar()
        assert scrollbar is not None
        scrollbar.setRange(0, 50)
        panel._scroll_to_end()
        self.app.processEvents()  # Runs the deferred scroll
        self.assertEqual(scrollbar.value(), scrollbar.maximum())

    def test_screenshots_open_enlarged(self) -> None:
        """Open a sent message's screenshots in the viewer."""
        with patch.object(self.window.screenshot_viewer, "open_screenshots") as viewer:
            self.window._open_screenshots(["a.png"], 0)
        viewer.assert_called_once_with(["a.png"], 0)


class SpeechTests(WindowTestCase):
    """Read the newest reply, or a clicked line, aloud as it streams."""

    def setUp(self) -> None:
        """Replace the speaker so nothing plays, and record what the chat reports."""
        super().setUp()
        self.speaker = MagicMock()
        self.window.speaker = self.speaker
        self.changes: list[tuple[object, str, bool]] = []
        self.window.chat_area.reply_changed.connect(
            lambda reply, text, complete: self.changes.append((reply, text, complete))
        )

    def test_empty_chat_reads_nothing(self) -> None:
        """Do nothing when there is no reply."""
        self.window.speak_latest()
        self.speaker.speak.assert_not_called()

    def test_pending_reply_is_read_as_it_streams(self) -> None:
        """Read a pending reply, then report each chunk and its end."""
        chat = self.window.chat_area
        chat.add_message("Question?", is_user=True)
        self.window.speak_latest()
        reply = chat.streaming_bubble
        self.speaker.speak.assert_called_once_with(reply, "", False)
        chat.append_to_stream("Hello ")
        chat.append_to_stream("there.")
        chat.finalize_assistant_stream()
        self.assertEqual(
            self.changes,
            [
                (reply, "Hello ", False),
                (reply, "Hello there.", False),
                (reply, "Hello there.", True),
            ],
        )
        self.window.speak_latest()
        self.speaker.speak.assert_called_with(reply, "Hello there.", True)

    def test_stopped_and_failed_replies_end_speech_at_their_text(self) -> None:
        """End speech where a stopped or failed reply ended, never reading the error."""
        chat = self.window.chat_area
        chat.add_message("One?", is_user=True)
        chat.append_to_stream("Partial ")
        chat.stop_assistant_stream()
        chat.add_message("Two?", is_user=True)
        chat.append_to_stream("Before ")
        chat.show_stream_error("Error generating response: boom")
        self.assertEqual(
            [(text, complete) for _reply, text, complete in self.changes if complete],
            [("Partial ", True), ("Before ", True)],
        )

    def test_clearing_the_chat_stops_speech(self) -> None:
        """Stop reading when a new chat deletes the reply."""
        cleared: list[bool] = []
        self.window.chat_area.messages_cleared.connect(lambda: cleared.append(True))
        self.window.chat_area.clear_chat_messages()
        self.assertEqual(cleared, [True])

    def test_clicked_line_is_read_from_its_start(self) -> None:
        """Read from the start of the clicked line of a reply."""
        chat = self.window.chat_area
        chat.load_messages(
            [
                ("Q", True, "", ()),
                ("First paragraph.\n\nSecond paragraph.", False, "", ()),
            ]
        )
        self.window.show()
        self.app.processEvents()
        reply = chat.latest_reply()
        assert reply is not None
        label = reply.message_label
        bottom = label.mapToGlobal(QPoint(5, label.height() - 3))
        self.window.speak_from(reply, bottom)
        args = self.speaker.speak.call_args.args
        self.assertIs(args[0], reply)
        self.assertEqual(args[1][args[3] :], "Second paragraph.")
        self.assertTrue(args[2])
        with patch.object(label, "findChildren", return_value=[]):
            self.assertIsNone(reply.line_at(bottom))
            self.window.speak_from(reply, bottom)
        self.assertEqual(self.speaker.speak.call_count, 1)

    def test_speech_failures_are_shown(self) -> None:
        """Show the reason a speech session stopped."""
        self.window.settings_page.speaker.failed.emit("No device.")
        self.assertIn("No device.", self.warnings)


if __name__ == "__main__":
    unittest.main()
