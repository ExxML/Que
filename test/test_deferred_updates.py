"""Check that queued scrolls and resizes are dropped once their widget is deleted."""

import unittest
from collections.abc import Callable
from typing import cast

from PyQt6 import sip
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget
from qt_application import application

from ui.chat.chat_area import ChatArea
from ui.input.input_field import InputField

# PyQt6's stubs type QTest's C++ namespace functions as instance methods.
wait = cast(Callable[[int], None], QTest.qWait)


class DeferredUpdateTests(unittest.TestCase):
    """Never run a queued update against a deleted widget, which crashes the process."""

    def test_deleted_widgets_skip_queued_updates(self) -> None:
        """Delete widgets with scrolls and height changes still queued, then run the event loop."""
        application()
        parent = QWidget()
        chat = ChatArea(parent)
        chat.load_messages([("Saved", True, "", ())])
        chat.add_message("New", True)
        chat.append_to_stream("Reply")
        field = InputField(parent)
        field.input_field._adjust_height()
        field.input_field.setPlainText("one\ntwo\nthree")
        field.input_field._adjust_height()  # Queues the height change
        sip.delete(parent)
        wait(20)  # Let any queued scroll or resize callbacks run
        self.assertTrue(sip.isdeleted(chat))
        self.assertTrue(sip.isdeleted(field))

    def test_wrappers_of_qt_created_widgets_see_their_deletion(self) -> None:
        """Mark the chat's Qt-created scroll bar deleted, which fails once any earlier test destroyed the app."""
        application()
        parent = QWidget()
        chat = ChatArea(parent)
        sip.delete(parent)
        self.assertTrue(sip.isdeleted(chat.scrollbar))


if __name__ == "__main__":
    unittest.main()
