"""Verify chat message layout and scrolling."""

import unittest
from collections.abc import Callable
from typing import cast

from PyQt6.QtGui import QTextDocument
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from qt_application import application

from ui.chat.chat_area import ChatArea
from ui.chat.chat_bubble import ChatBubble

wait = cast(Callable[[int], None], QTest.qWait)


class ChatWrapTests(unittest.TestCase):
    """Check that message lines follow the chat width both wider and narrower."""

    def setUp(self) -> None:
        """Create the shared application."""
        self.app = application()

    def test_messages_rewrap_with_window_width(self) -> None:
        """Widen long messages and shorten their wrapped height as the window grows, and reverse it."""
        window = QWidget()
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        chat = ChatArea(window)
        layout.addWidget(chat)
        chat.load_messages(
            [
                ("word " * 200, True, "", ()),
                ("short", True, "", ()),
                ("reply " * 300, False, "", ()),
            ]
        )
        window.show()
        self.addCleanup(window.close)

        def labels(width: int) -> list[tuple[int, int]]:
            """Resize the window and report each message's width and height.

            Args:
                width (int): The window width.

            Returns:
                list[tuple[int, int]]: Each message label's width and height.
            """
            window.resize(width, 600)
            self.app.processEvents()
            return [
                (b.message_label.width(), b.message_label.height())
                for b in chat.chat_container.findChildren(ChatBubble)
            ]

        default = labels(550)
        # Replies keep their original width by default
        self.assertEqual(default[2][0], 515)
        wide = labels(900)
        narrow = labels(300)
        for index in (0, 2):
            self.assertGreater(wide[index][0], default[index][0])
            self.assertLess(wide[index][1], default[index][1])
            self.assertLess(narrow[index][0], default[index][0])
            self.assertGreater(narrow[index][1], default[index][1])
            self.assertLessEqual(narrow[index][0], chat.chat_container.width())
        self.assertEqual(wide[1], default[1])  # Short messages keep their natural size
        self.assertEqual(labels(550), default)

        # Each message is laid out at its real width only, never at a degenerate zero width
        for bubble in chat.chat_container.findChildren(ChatBubble):
            self.assertTrue(all(width > 0 for width in bubble.message_label.heights))

    def test_transcript_width_follows_the_message(self) -> None:
        """Update a message's attached transcript width when the chat is resized."""
        bubble = ChatBubble("Question", is_user=True, transcript="Transcript " * 40)
        self.addCleanup(bubble.close)
        transcript = bubble.transcript_label
        assert transcript is not None

        bubble.resize(600, 300)
        bubble.show()
        self.assertEqual(transcript.maximumWidth(), 450)

        bubble.resize(300, 300)
        self.assertEqual(transcript.maximumWidth(), 225)

    def test_long_words_wrap_instead_of_widening_replies(self) -> None:
        """Break words wider than a narrow reply so no line, in a list or not, runs past the window."""
        window = QWidget()
        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        chat = ChatArea(window)
        layout.addWidget(chat)
        code = "`homePage.tapWorkoutsTab().tapFirstWorkout().assertTitle()`"
        chat.load_messages(
            [
                (f"Then {code} chains calls.", False, "", ()),
                (
                    f"- **Structure:**\n  - Tests read like {code} so they stay short.",
                    False,
                    "",
                    (),
                ),
                (f"| Call | Use |\n|---|---|\n| {code} | Chains |", False, "", ()),
                ("https://example.com/" + "segment" * 30, True, "", ()),
            ]
        )
        window.resize(300, 600)
        window.show()
        self.addCleanup(window.close)
        self.app.processEvents()

        for bubble in chat.chat_container.findChildren(ChatBubble):
            label = bubble.message_label
            document = label.findChild(QTextDocument)
            assert document is not None
            self.assertLessEqual(document.size().width(), label.contentsRect().width())

    def test_message_heights_are_measured_once_per_text(self) -> None:
        """Reuse a measured height until the text changes."""
        bubble = ChatBubble("")
        bubble.set_bot_message("reply " * 300)
        label = bubble.message_label
        height = label.heightForWidth(500)
        # A cached height is returned without laying the text out again
        label.heights[500] = -1
        self.assertEqual(label.heightForWidth(500), -1)
        bubble.set_bot_message("reply " * 300)
        self.assertEqual(label.heightForWidth(500), height)
        bubble.set_bot_message("reply " * 600)
        self.assertGreater(label.heightForWidth(500), height)


class ChatScrollTests(unittest.TestCase):
    """Check scrolling for sent messages and the start of streamed replies."""

    def setUp(self) -> None:
        """Show a chat viewport and lay out its contents."""
        application()
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.chat = ChatArea(self.window)
        layout.addWidget(self.chat)
        self.window.resize(440, 300)
        self.window.show()
        self.addCleanup(self.window.close)
        wait(20)

    def test_first_text_scrolls_once_and_keeps_manual_position(self) -> None:
        """Reveal the sent message, align the reply, and respect later manual scrolling."""
        self.chat.add_message("A long question " * 40, is_user=True)
        item = self.chat.chat_layout.itemAt(0)
        assert item is not None
        sent_bubble = item.widget()
        assert sent_bubble is not None
        viewport = self.chat.viewport()
        assert viewport is not None
        wait(20)
        sent_position = self.chat.scrollbar.value()
        self.assertGreater(sent_position, 0)
        self.assertEqual(
            sent_bubble.y() + sent_bubble.height() - sent_position,
            viewport.height() - self.chat.fade_effect.FADE_HEIGHT,
        )

        self.chat.set_stream_status("Reading a file")
        self.chat.append_to_stream("\n ")
        wait(100)
        self.assertEqual(self.chat.scrollbar.value(), sent_position)

        self.chat.append_to_stream("Hello")
        bubble = self.chat.streaming_bubble
        assert bubble is not None
        wait(20)
        start = self.chat.scrollbar.value()
        self.assertGreater(start, 0)
        self.assertEqual(bubble.y() - start, self.chat.fade_effect.FADE_HEIGHT)
        self.assertEqual(bubble.message, "\n Hello")

        range_changes = QSignalSpy(self.chat.scrollbar.rangeChanged)
        self.chat.append_to_stream(" more words" * 100)
        if self.chat.scrollbar.maximum() <= start:
            self.assertTrue(range_changes.wait(2000), "The streamed reply did not grow")
        self.assertGreater(self.chat.scrollbar.maximum(), start)
        self.assertEqual(self.chat.scrollbar.value(), start)
        manual_position = start // 2
        self.chat.scrollbar.setValue(manual_position)
        self.chat.append_to_stream(" End.")
        self.chat.finalize_assistant_stream()
        wait(80)
        self.assertEqual(self.chat.scrollbar.value(), manual_position)
        self.assertTrue(bubble.message.endswith(" End."))

        scrolls: list[int] = []
        self.chat.scrollbar.valueChanged.connect(scrolls.append)
        self.chat.add_message("Follow-up", is_user=True)
        self.chat.append_to_stream("Second reply")
        next_bubble = self.chat.streaming_bubble
        assert next_bubble is not None
        wait(20)
        self.assertEqual(
            next_bubble.y() - self.chat.scrollbar.value(),
            self.chat.fade_effect.FADE_HEIGHT,
        )
        self.assertEqual(len(scrolls), 1)

    def test_reply_finished_while_hidden_scrolls_when_shown(self) -> None:
        """Keep the scroll pending when a short reply finishes offscreen."""
        self.window.hide()
        self.chat.add_message("A long question " * 40, is_user=True)
        self.chat.append_to_stream("Hello")
        bubble = self.chat.streaming_bubble
        assert bubble is not None
        self.chat.finalize_assistant_stream()
        wait(20)
        self.assertEqual(self.chat.scrollbar.value(), 0)

        self.window.show()
        wait(20)
        self.assertEqual(
            bubble.y() - self.chat.scrollbar.value(), self.chat.fade_effect.FADE_HEIGHT
        )

    def test_sent_message_scrolls_when_shown_before_reply(self) -> None:
        """Reveal a new message below existing history when the chat is reopened."""
        self.chat.load_messages([("Previous reply " * 100, False, "", ())])
        wait(20)
        self.chat.scrollbar.setValue(0)
        self.window.hide()
        self.chat.add_message("Follow-up", is_user=True)
        wait(20)
        self.assertEqual(self.chat.scrollbar.value(), 0)

        item = self.chat.chat_layout.itemAt(1)
        assert item is not None
        sent_bubble = item.widget()
        assert sent_bubble is not None
        viewport = self.chat.viewport()
        assert viewport is not None
        self.window.show()
        wait(20)
        self.assertGreater(self.chat.scrollbar.value(), 0)
        self.assertEqual(
            sent_bubble.y() + sent_bubble.height() - self.chat.scrollbar.value(),
            viewport.height() - self.chat.fade_effect.FADE_HEIGHT,
        )

    def test_clear_cancels_queued_reply_scroll(self) -> None:
        """Drop the pending jump and its padding when the chat is cleared."""
        self.chat.add_message("First question", is_user=True)
        self.chat.append_to_stream("First reply")
        wait(20)
        self.assertGreater(self.chat.scrollbar.maximum(), 0)
        self.chat.add_message("Second question", is_user=True)
        self.chat.append_to_stream("Second reply")
        self.window.resize(500, 320)
        self.chat.clear_chat_messages()
        wait(20)
        self.assertEqual(self.chat.scrollbar.maximum(), 0)

    def test_resize_refits_scroll_space_for_rewrapped_messages(self) -> None:
        """Keep a short reply visible when earlier messages rewrap to fewer lines."""
        self.chat.add_message("A long question " * 40, is_user=True)
        self.chat.append_to_stream("Hello")
        bubble = self.chat.streaming_bubble
        assert bubble is not None
        wait(20)
        previous_top = bubble.y()
        self.window.resize(900, 300)
        wait(20)
        self.assertLess(bubble.y(), previous_top)
        self.assertEqual(
            bubble.y() - self.chat.scrollbar.value(), self.chat.fade_effect.FADE_HEIGHT
        )


if __name__ == "__main__":
    unittest.main()
