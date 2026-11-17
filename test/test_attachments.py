"""Verify that sent screenshots are stored with chat history and shown with their message."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import QAbstractButton, QApplication, QPushButton, QWidget

from core.chat_history import ChatHistory, Conversation
from core.claude_bridge import ClaudeBridge
from core.settings import SettingsStore
from ui.chat.chat_area import ChatArea
from ui.chat.chat_bubble import ChatBubble
from ui.theme import DANGER_COLOR, qcolor
from ui.window.screenshot_viewer import ScreenshotViewer
from ui.window.settings_page import SettingsPage


def _image(path: Path) -> str:
    """Write a small solid image file.

    Args:
        path (Path): Where to write the image.

    Returns:
        str: The image path.
    """
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(QColor(200, 30, 30))
    image.save(str(path))
    return str(path)


class ScreenshotHistoryTests(unittest.TestCase):
    """Exercise moving, naming, deleting, and cleaning stored screenshots."""

    def setUp(self) -> None:
        """Create an isolated history location and a capture cache."""
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.path = self.root / "chat_history.json"

    def test_sent_screenshots_move_into_history_and_resolve_back(self) -> None:
        """Move captures out of the cache and resolve the saved names to the same files."""
        history = ChatHistory(self.path)
        capture = _image(self.cache / "screenshot0.png")
        [kept] = history.keep_screenshots([capture])
        self.assertFalse(Path(capture).exists())
        self.assertTrue(Path(kept).exists())
        self.assertEqual(Path(kept).parent, history.screenshots_dir)
        names = history.screenshot_names([kept, str(self.root / "notes.txt")])
        self.assertEqual(names, [Path(kept).name])
        self.assertEqual(history.screenshot_paths({"screenshots": names}), (kept,))

    def test_unmovable_screenshot_is_still_sent_but_not_saved(self) -> None:
        """Keep the original path of a file that cannot move so the turn still attaches it."""
        history = ChatHistory(self.path)
        missing = str(self.cache / "screenshot9.png")
        self.assertEqual(history.keep_screenshots([missing]), [missing])
        self.assertEqual(history.screenshot_names([missing]), [])

    def test_names_outside_storage_are_never_resolved(self) -> None:
        """Ignore saved names that could point outside screenshot storage."""
        history = ChatHistory(self.path)
        message = {"screenshots": ["../chat_history.json", "a" * 32 + ".png", 5]}
        self.assertEqual(
            history.screenshot_paths(message),
            (str(history.screenshots_dir / ("a" * 32 + ".png")),),
        )

    def test_deleting_a_conversation_deletes_its_screenshots(self) -> None:
        """Remove a deleted chat's screenshots and keep other chats' screenshots."""
        history = ChatHistory(self.path)
        first, second = history.keep_screenshots(
            [_image(self.cache / "a.png"), _image(self.cache / "b.png")]
        )
        deleted = Conversation(
            "gemini",
            "Deleted",
            messages=[{"role": "user", "text": "x", "screenshots": [Path(first).name]}],
        )
        kept = Conversation(
            "gemini",
            "Kept",
            messages=[
                {"role": "user", "text": "y", "screenshots": [Path(second).name]}
            ],
        )
        history.save(deleted)
        history.save(kept)
        history.delete(deleted.conversation_id)
        self.assertFalse(Path(first).exists())
        self.assertTrue(Path(second).exists())

    def test_startup_removes_only_old_unreferenced_screenshots(self) -> None:
        """Clean failed sends' old screenshots, keeping referenced, recent, and foreign files."""
        history = ChatHistory(self.path)
        referenced, orphan, recent = history.keep_screenshots(
            [_image(self.cache / f"{name}.png") for name in ("r", "o", "n")]
        )
        foreign = _image(history.screenshots_dir / "screenshot0.png")
        old = time.time() - 2 * 86400
        for path in (referenced, orphan, foreign):
            os.utime(path, (old, old))
        history.save(
            Conversation(
                "gemini",
                "Chat",
                messages=[
                    {
                        "role": "user",
                        "text": "x",
                        "screenshots": [Path(referenced).name],
                    }
                ],
            )
        )
        ChatHistory(self.path)
        self.assertTrue(Path(referenced).exists())
        self.assertFalse(Path(orphan).exists())
        self.assertTrue(Path(recent).exists())
        self.assertTrue(Path(foreign).exists())

    def test_unreadable_history_never_removes_screenshots(self) -> None:
        """Keep every screenshot when the history file cannot prove what is unreferenced."""
        history = ChatHistory(self.path)
        [stored] = history.keep_screenshots([_image(self.cache / "a.png")])
        old = time.time() - 2 * 86400
        os.utime(stored, (old, old))
        self.path.write_text("{not json", encoding="utf-8")
        ChatHistory(self.path)
        self.assertTrue(Path(stored).exists())

    def test_settings_measure_and_delete_all_after_confirmation(self) -> None:
        """Show the stored size and delete every screenshot only on the confirming click."""
        history = ChatHistory(self.path)
        stored = history.keep_screenshots(
            [_image(self.cache / "a.png"), _image(self.cache / "b.png")]
        )
        size = sum(Path(path).stat().st_size for path in stored)
        window = QWidget()
        self.addCleanup(window.deleteLater)
        page = SettingsPage(
            SettingsStore(self.root / "config.json"),
            ClaudeBridge(),
            MagicMock(),
            history,
            window,
        )
        self.addCleanup(page.key_timer.stop)
        self.assertEqual(history.screenshots_size(), size)
        self.assertEqual(
            page.screenshots_size_label.text(), f"Screenshots use {size} B."
        )
        page.delete_screenshots_button.click()
        self.assertTrue(all(Path(path).exists() for path in stored))
        page.tab_bar.setCurrentIndex(1)  # Switching tabs cancels the pending delete
        page.delete_screenshots_button.click()
        self.assertTrue(all(Path(path).exists() for path in stored))
        page.delete_screenshots_button.click()
        self.assertFalse(any(Path(path).exists() for path in stored))
        self.assertEqual(page.screenshots_size_label.text(), "Screenshots use 0 B.")
        self.assertFalse(page.delete_screenshots_button.isEnabled())
        # The confirming red style must not outlive the deletion
        self.assertNotEqual(
            page.delete_screenshots_button.palette().buttonText().color(),
            qcolor(DANGER_COLOR),
        )


class ScreenshotDisplayTests(unittest.TestCase):
    """Show sent screenshots under the user message and open them in the viewer."""

    def setUp(self) -> None:
        """Create screenshot files to display."""
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.paths = [_image(root / f"{index}.png") for index in range(8)]

    def test_previews_fit_the_bubble_and_count_the_rest(self) -> None:
        """Show as many previews as fit and open the clicked one by its position."""
        bubble = ChatBubble("Look", True, "", tuple(self.paths))
        previews = [
            child
            for child in bubble.findChildren(QAbstractButton)
            if child.accessibleName().startswith("View screenshot")
        ]
        self.assertEqual(len(previews), 5)
        self.assertEqual(previews[-1].toolTip(), "View 8 screenshots")
        self.assertEqual(previews[0].toolTip(), "View screenshot")
        opened: list[tuple[list[str], int]] = []
        bubble.screenshot_opened.connect(
            lambda paths, index: opened.append((paths, index))
        )
        previews[1].click()
        self.assertEqual(opened, [(self.paths, 1)])

    def test_message_without_attachments_keeps_its_plain_layout(self) -> None:
        """Add no previews or transcript toggle to a plain user message."""
        bubble = ChatBubble("Plain", True)
        self.assertEqual(bubble.findChildren(QPushButton), [])
        self.assertFalse(
            any(
                c.accessibleName().startswith("View")
                for c in bubble.findChildren(QWidget)
            )
        )

    def test_chat_area_forwards_saved_and_new_screenshots(self) -> None:
        """Forward preview clicks from both new and reloaded messages."""
        parent = QWidget()
        chat = ChatArea(parent)
        opened: list[int] = []
        chat.screenshot_opened.connect(lambda _paths, index: opened.append(index))
        chat.load_messages(
            [("Saved", True, "", (self.paths[0],)), ("Reply", False, "", ())]
        )
        chat.add_message("New", True, "", (self.paths[1], self.paths[2]))
        bubbles = chat.chat_container.findChildren(ChatBubble)
        for bubble in bubbles:
            for child in bubble.findChildren(QAbstractButton):
                if child.accessibleName().startswith("View screenshot"):
                    child.click()
        self.assertEqual(opened, [0, 0, 1])

    def test_viewer_steps_wraps_and_closes(self) -> None:
        """Step with the arrow keys, wrap at the ends, and close on Escape or a click."""
        window = QWidget()
        window.resize(400, 300)
        viewer = ScreenshotViewer(window, lambda _painter: None)
        viewer.setGeometry(0, 0, 400, 300)
        window.show()
        viewer.open_screenshots(self.paths[:3], 2)
        self.assertTrue(viewer.isVisible())
        self.assertEqual(viewer.counter.text(), "3 / 3")
        target = QWidget(window)
        QApplication.sendEvent(
            target,
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier
            ),
        )
        self.assertEqual(viewer.index, 0)
        viewer.previous_button.click()
        self.assertEqual(viewer.index, 2)
        QApplication.sendEvent(
            target,
            QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
            ),
        )
        self.assertFalse(viewer.isVisible())
        viewer.open_screenshots(self.paths[:1], 0)
        self.assertFalse(viewer.next_button.isVisibleTo(viewer))
        QApplication.sendEvent(
            viewer,
            QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        self.assertFalse(viewer.isVisible())
        window.close()


if __name__ == "__main__":
    unittest.main()
