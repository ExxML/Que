"""Tests for routing owned keys and clicks to Que's widgets without activating its windows.

Que never takes operating-system focus, so a page cannot see a blur or focus change. The
router stands in for Windows: it moves Que's local focus, closes popups, runs frame buttons
and drags, and translates keys. These tests drive those paths with real widgets and fake
only the two Win32 calls that need a native window (coordinate mapping and hit-testing).
"""

import unittest
from collections.abc import Callable
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt
from PyQt6.QtGui import QHideEvent, QKeyEvent, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qt_application import application

from core.model_catalog import ModelOption
from core.window_policy import native
from ui.input.input_field import InputField
from ui.input.input_router import InputRouter, KeyTranslator

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x200, 0x201, 0x202
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_XBUTTONDOWN = 0x204, 0x205, 0x20B
HTCAPTION, HTMINBUTTON, HTMAXBUTTON, HTCLOSE, HTLEFT = 2, 8, 9, 20, 10


class RecordingWidget(QWidget):
    """Record the routed events a widget receives."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[QEvent.Type] = []
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def event(self, event: QEvent | None) -> bool:
        """Record the event type, then handle it normally.

        Args:
            event (QEvent): The delivered event.

        Returns:
            bool: Whether the event was handled.
        """
        if event is not None:
            self.received.append(event.type())
        return super().event(event)


class RouterTestCase(unittest.TestCase):
    """Build a real router with a fake shortcut manager and input helper."""

    def setUp(self) -> None:
        """Create the router, a main window, and the faked native calls."""
        self.app = application()
        self.main_window = QWidget()
        self.main_window.resize(400, 300)
        self.shortcuts = MagicMock()
        self.shortcuts.main_window = self.main_window
        self.main_window.ai_sender = MagicMock()  # type: ignore[attr-defined]
        self.main_window.ai_sender.settings_store.click_through.return_value = False  # type: ignore[attr-defined]
        self.main_window.mouse_routing_changed = MagicMock()  # type: ignore[attr-defined]
        self.shortcuts.hook_client.held_modifiers = ()
        self.hit = 1  # HTCLIENT
        for name, value in (
            (
                "ScreenToClient",
                lambda _hwnd, _point: 1,
            ),  # Desktop coordinates are already local
            ("SendMessageW", lambda *_args: self.hit),
        ):
            patcher = patch.object(native, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.router = InputRouter(self.app, self.shortcuts)
        self.addCleanup(self.router.close)
        refresh = patch.object(
            self.router, "refresh_regions"
        )  # Offscreen windows have no native rectangle
        refresh.start()
        self.addCleanup(refresh.stop)
        self.addCleanup(self.main_window.close)

    def window(self, widget: QWidget) -> int:
        """Register a shown top-level window with the router as if it were hit-tested.

        Args:
            widget (QWidget): The top-level window.

        Returns:
            int: Its handle.
        """
        widget.show()
        self.addCleanup(widget.close)
        hwnd = int(widget.winId())
        self.router.windows[hwnd] = widget
        return hwnd

    def key(
        self, vk: int, down: bool = True, held: tuple[int, ...] = (), flags: int = 0
    ) -> None:
        """Route one key event.

        Args:
            vk (int): The virtual key.
            down (bool): Whether it is a press.
            held (tuple[int, ...]): The held modifiers.
            flags (int): The low-level key flags.
        """
        self.router.on_key(vk, 0, flags, down, False, held)


class KeyboardFocusTests(RouterTestCase):
    """Move local focus the way Windows would, without moving real focus."""

    def setUp(self) -> None:
        """Create a window with three fields, the middle one disabled."""
        super().setUp()
        layout = QVBoxLayout(self.main_window)
        self.fields = [QLineEdit(), QLineEdit(), QLineEdit()]
        for field in self.fields:
            layout.addWidget(field)
        self.fields[1].setEnabled(False)
        self.window(self.main_window)

    def test_tab_skips_disabled_fields_and_shift_tab_goes_back(self) -> None:
        """Tab to the next enabled field, and Shift+Tab back."""
        self.router._focus(self.fields[0])
        self.key(0x09)
        self.assertIs(self.router.target, self.fields[2])
        self.key(0x09, held=(0xA0,))
        self.assertIs(self.router.target, self.fields[0])

    def test_tab_with_nothing_else_focusable_stays(self) -> None:
        """Stop searching once the focus chain wraps back to the target."""
        self.fields[2].setEnabled(False)
        self.router._focus(self.fields[0])
        self.key(0x09)
        self.assertIs(self.router.target, self.fields[0])

    def test_escape_leaves_typing_mode(self) -> None:
        """Deliver Escape, then stop routing keys to Que."""
        self.router._focus(self.fields[0])
        self.key(0x1B)
        self.assertIsNone(self.router.target)
        self.shortcuts.arm_input.assert_called_with(False)

    def test_escape_closes_the_top_popup_first(self) -> None:
        """Close an open popup instead of leaving typing mode."""
        self.router._focus(self.fields[0])
        popup = QWidget()
        popup.setProperty("quePopup", True)
        self.window(popup)
        with patch.object(self.router, "_top_popup", return_value=popup):
            self.router.on_key(0x1B, 0, 0, True, False, ())
        self.assertFalse(popup.isVisible())
        self.assertIs(self.router.target, self.fields[0])

    def test_pending_dead_key_sends_nothing(self) -> None:
        """Hold a dead key back until the next key completes it."""
        self.router._focus(self.fields[0])
        with (
            patch.object(self.router.translator, "translate", return_value=None),
            patch.object(QApplication, "sendEvent") as send,
        ):
            self.key(0x41)
        send.assert_not_called()

    def test_keys_without_a_target_go_nowhere(self) -> None:
        """Deliver nothing while no Que widget has local focus."""
        with patch.object(QApplication, "sendEvent") as send:
            self.key(0x41)
        send.assert_not_called()

    def test_turning_routing_off_disarms(self) -> None:
        """Stop typing into Que when interaction routing is switched off."""
        self.router._focus(self.fields[0])
        self.router.set_enabled(False)
        self.assertIsNone(self.router.target)
        self.shortcuts.hook_client.set_routing.assert_called_with(False)

    def test_closed_router_publishes_no_regions(self) -> None:
        """Publish no hit regions after shutdown, so the helper claims no clicks."""
        self.router.close()
        self.shortcuts.hook_client.set_regions.reset_mock()
        InputRouter.refresh_regions(self.router)  # The real method, not the setUp patch
        self.shortcuts.hook_client.set_regions.assert_not_called()

    def test_helper_ending_typing_mode_disarms(self) -> None:
        """Leave local editing when the helper ends typing mode, e.g. for Alt+Tab."""
        self.router._focus(self.fields[0])
        self.router.on_armed_changed(True)
        self.assertIs(self.router.target, self.fields[0])
        self.router.on_armed_changed(False)
        self.assertIsNone(self.router.target)

    def test_hiding_the_overlay_disarms_and_closes_popups(self) -> None:
        """Drop focus and popups when the main window hides, and refresh on window changes."""
        self.router._focus(self.fields[0])
        self.router.refresh_pending = False
        self.assertFalse(self.router.eventFilter(self.fields[0], QShowEvent()))
        self.assertTrue(self.router.refresh_pending)
        with patch.object(self.router, "_close_popups") as close:
            self.router.eventFilter(self.main_window, QHideEvent())
        close.assert_called_once()
        self.assertIsNone(self.router.target)
        self.assertFalse(self.router.eventFilter(None, None))


class SendButtonTests(RouterTestCase):
    """Exercise both input actions through the overlay's pointer routing."""

    def test_routed_send_and_answer(self) -> None:
        """Send a draft and answer only on a separate click without arming typing."""
        field = InputField(self.main_window)
        edit = field.input_field
        layout = QVBoxLayout(self.main_window)
        layout.addWidget(field)
        hwnd = self.window(self.main_window)
        button = field.send_button
        width = button.width()
        sent, answered = MagicMock(), MagicMock()
        field.message_sent.connect(sent)
        field.answer_requested.connect(answered)

        def click() -> None:
            """Deliver a routed press and release at the button center."""
            point = button.mapTo(self.main_window, button.rect().center())
            scale = self.main_window.devicePixelRatioF()
            x, y = round(point.x() * scale), round(point.y() * scale)
            self.router.on_pointer(WM_LBUTTONDOWN, x, y, 0, hwnd)
            self.router.on_pointer(WM_LBUTTONUP, x, y, 0, hwnd)

        edit.setPlainText("  hello  ")
        self.assertEqual(button.text(), "Send")
        self.assertEqual(button.width(), width)
        self.assertFalse(button.isEnabled())
        click()
        self.assertEqual(edit.toPlainText(), "  hello  ")
        sent.assert_not_called()
        answered.assert_not_called()

        field.input_settings.set_models((ModelOption("test", "Test", ("default",)),))
        self.assertTrue(button.isEnabled())
        click()
        # Force the next press at the same position to be a double-click.
        with patch.object(QApplication, "doubleClickInterval", return_value=10_000):
            click()
        sent.assert_called_once_with("hello")
        answered.assert_not_called()
        self.assertEqual(edit.toPlainText(), "")
        self.assertEqual(button.accessibleName(), "Answer")

        edit.setPlainText("\n  ")
        self.router._focus(edit)
        self.key(0x0D)
        answered.assert_not_called()
        click()
        answered.assert_called_once_with()
        sent.assert_called_once_with("hello")
        self.assertIsNone(self.router.target)

        edit.setPlainText("draft during reload")
        self.assertTrue(button.isEnabled())
        field.input_settings.show_loading()
        self.assertFalse(button.isEnabled())


class PointerTests(RouterTestCase):
    """Deliver clicks to Que's widgets, and run its frame buttons and drags."""

    def setUp(self) -> None:
        """Show a main window holding one recording widget."""
        super().setUp()
        self.widget = RecordingWidget()
        layout = QVBoxLayout(self.main_window)
        layout.addWidget(self.widget)
        self.hwnd = self.window(self.main_window)
        self.inside = self.widget.mapTo(self.main_window, self.widget.rect().center())

    def press(self, message: int, point: QPoint, data: int = 0) -> None:
        """Route one pointer message at a window-local point.

        Args:
            message (int): The native mouse message.
            point (QPoint): The window-local position.
            data (int): The mouse data.
        """
        self.router.on_pointer(message, point.x(), point.y(), data, self.hwnd)

    def test_right_click_opens_the_context_menu(self) -> None:
        """Send a context-menu event after a routed right-button release."""
        self.press(WM_RBUTTONDOWN, self.inside)
        self.press(WM_RBUTTONUP, self.inside)
        self.assertIn(QEvent.Type.ContextMenu, self.widget.received)

    def test_back_and_forward_buttons_are_told_apart(self) -> None:
        """Map the first extra button to Back and the second to Forward."""
        self.press(WM_XBUTTONDOWN, self.inside, 1 << 16)
        self.press(WM_XBUTTONDOWN, self.inside, 2 << 16)
        self.assertEqual(set(self.router.pressed), {8, 16})

    def test_press_on_a_disabled_widget_disarms(self) -> None:
        """Ignore presses on disabled widgets and leave typing mode."""
        self.widget.setEnabled(False)
        self.main_window.setEnabled(False)
        self.press(WM_LBUTTONDOWN, self.inside)
        self.assertFalse(self.router.pressed)
        self.shortcuts.arm_input.assert_called_with(False)

    def test_modal_dialog_blocks_other_windows(self) -> None:
        """Drop input to other windows while a modal dialog is open."""
        dialog = QDialog()
        with patch.object(QApplication, "activeModalWidget", return_value=dialog):
            self.press(WM_LBUTTONDOWN, self.inside)
        self.assertFalse(self.router.pressed)

    def test_frame_buttons_minimise_maximise_and_close(self) -> None:
        """Run a frame button only if the release is still over it."""
        outside = QPoint(10, -5)
        checks: list[tuple[int, Callable[[], bool]]] = [
            (HTMAXBUTTON, lambda: self.main_window.isMaximized()),
            (HTMAXBUTTON, lambda: not self.main_window.isMaximized()),
            (HTMINBUTTON, lambda: self.main_window.isMinimized()),
            (HTCLOSE, lambda: not self.main_window.isVisible()),
        ]
        for hit, check in checks:
            with self.subTest(hit=hit):
                self.hit = hit
                self.press(WM_LBUTTONDOWN, outside)
                self.assertEqual(self.router.frame_action, (self.main_window, hit))
                self.press(WM_LBUTTONUP, outside)
                self.assertIsNone(self.router.frame_action)
                self.assertTrue(check())
                if hit == HTMINBUTTON:
                    self.main_window.showNormal()

    def test_frame_button_release_elsewhere_does_nothing(self) -> None:
        """Cancel a frame button when the pointer leaves it before release."""
        self.hit = HTCLOSE
        self.press(WM_LBUTTONDOWN, QPoint(10, -5))
        self.hit = HTCAPTION
        self.press(WM_LBUTTONUP, QPoint(10, -5))
        self.assertTrue(self.main_window.isVisible())

    def test_caption_and_edge_presses_start_drags(self) -> None:
        """Move by the caption and resize by the native edges."""
        for hit, edge in ((HTCAPTION, ""), (HTLEFT, "l")):
            with self.subTest(hit=hit):
                self.hit = hit
                self.press(WM_LBUTTONDOWN, QPoint(-3, 10))
                assert self.router.drag is not None
                self.assertEqual(self.router.drag[3], edge)
                self.press(WM_LBUTTONUP, QPoint(-3, 10))
                self.assertIsNone(self.router.drag)

    def test_top_left_resize_keeps_a_minimum_size(self) -> None:
        """Resize from the top-left corner without shrinking below the minimum."""
        start = self.main_window.geometry()
        self.router.drag = (self.main_window, QPoint(0, 0), QRect(start), "tl")
        self.router._drag_to(QPoint(-20, -10))
        self.assertEqual(
            self.main_window.geometry().topLeft(), start.topLeft() + QPoint(-20, -10)
        )
        self.router._drag_to(QPoint(5000, 5000))
        self.assertGreaterEqual(self.main_window.width(), 200)
        self.assertLessEqual(
            self.main_window.width(), 201
        )  # QRect.setRight is inclusive
        self.assertGreaterEqual(self.main_window.height(), 150)
        self.assertLessEqual(self.main_window.height(), 151)
        self.router.drag = None
        self.router._drag_to(QPoint(0, 0))  # No drag: nothing to apply

    def test_hovering_the_same_widget_sends_no_new_enter(self) -> None:
        """Send enter events only when the hovered widget changes."""
        self.router._hover(self.widget, QPoint())
        count = self.widget.received.count(QEvent.Type.Enter)
        self.router._hover(self.widget, QPoint())
        self.assertEqual(self.widget.received.count(QEvent.Type.Enter), count)


class PopupTests(RouterTestCase):
    """Close Que's popups on outside presses without closing the popup being used."""

    def test_press_in_a_child_popup_keeps_its_parent_open(self) -> None:
        """Keep the popups a pressed popup belongs to, closing only unrelated ones."""
        parent, child, other = QWidget(), QWidget(), QWidget()
        child.setParent(parent, Qt.WindowType.Popup)
        for popup in (
            other,
            parent,
            child,
        ):  # Qt closes a Popup when a later window shows
            popup.setProperty("quePopup", True)
            self.window(popup)
        self.assertTrue(child.isVisible())
        self.router._close_popups(child)
        self.assertTrue(parent.isVisible())
        self.assertTrue(child.isVisible())
        self.assertFalse(other.isVisible())

    def test_outside_press_closes_other_popups_and_combo_lists(self) -> None:
        """Close every other popup, keep the pressed one, and close combo lists through the combo."""
        pressed, other = QWidget(), QWidget()
        for popup in (pressed, other):
            popup.setProperty("quePopup", True)
            self.window(popup)
        self.router._close_popups(pressed)
        self.assertTrue(pressed.isVisible())
        self.assertFalse(other.isVisible())

        combo = QComboBox()
        combo.addItems(["a", "b"])
        self.addCleanup(combo.close)
        list_window = QWidget(combo, Qt.WindowType.Popup)
        list_window.setProperty("quePopup", True)
        self.window(list_window)
        with patch.object(QComboBox, "hidePopup") as hide:
            self.router._close_popups()
        hide.assert_called()


class KeyTranslationTests(unittest.TestCase):
    """Translate physical keys the way the foreground layout would."""

    def setUp(self) -> None:
        """Create a translator."""
        application()
        self.translator = KeyTranslator()

    def translate(self, vk: int, down: bool = True, flags: int = 0) -> QKeyEvent:
        """Translate one key.

        Args:
            vk (int): The virtual key.
            down (bool): Whether it is a press.
            flags (int): The low-level key flags.

        Returns:
            QKeyEvent: The translated event.
        """
        event = self.translator.translate(vk, 0, flags, down, False, ())
        assert event is not None
        return event

    def test_function_keys_carry_no_text(self) -> None:
        """Map F1-F24 to Qt function keys."""
        event = self.translate(0x74)
        self.assertEqual((event.key(), event.text()), (int(Qt.Key.Key_F5), ""))

    def test_keypad_keys_and_keypad_enter_set_the_keypad_flag(self) -> None:
        """Mark numpad keys, and only the extended Enter, as keypad keys."""
        keypad = Qt.KeyboardModifier.KeypadModifier
        self.assertTrue(self.translate(0x60).modifiers() & keypad)
        self.assertTrue(self.translate(0x0D, flags=1).modifiers() & keypad)
        self.assertFalse(self.translate(0x0D).modifiers() & keypad)

    def test_space_release_keeps_its_text(self) -> None:
        """Give a space release its text, as Qt does for physical input."""
        self.assertEqual(self.translate(0x20, down=False).text(), " ")


if __name__ == "__main__":
    unittest.main()
