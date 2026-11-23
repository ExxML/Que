"""Verify that chat messages rewrap when the window is resized horizontally."""

import unittest

from PyQt6.QtWidgets import QVBoxLayout, QWidget
from qt_application import application

from ui.chat.chat_area import ChatArea
from ui.chat.chat_bubble import ChatBubble


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


if __name__ == "__main__":
    unittest.main()
