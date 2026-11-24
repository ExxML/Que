"""Tests that every hotkey action does what its shortcut promises.

The input helper decides which physical keys are Que's; ShortcutManager then runs the
action on the Qt thread. A hotkey that silently did nothing, or did the wrong thing, would
leave the user pressing it again in front of whoever is watching.
"""

import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QHideEvent, QShowEvent
from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.hook_client import HookClient
from core.hook_engine import ACTION_NAMES, PRESET_COUNT
from core.prompts import INTERVIEW_LABEL, INTERVIEW_PROMPT
from core.shortcut_manager import ShortcutManager

# shortcut: destroying a ShortcutManager's HookClient between tests aborts the process (0xC0000409);
# the app never destroys them, so tests keep theirs alive. Find the PyQt teardown cause if more tests need it.
KEPT: list[tuple[QWidget, ShortcutManager]] = []


class FakeMainWindow(QWidget):
    """A real top-level widget carrying the main window's collaborators as mocks."""

    def __init__(self) -> None:
        super().__init__()
        self.primary_screen = application().primaryScreen()
        self.ai_sender = MagicMock()
        self.ai_sender.settings_store.elevate_helper.return_value = False
        self.chat_area = MagicMock()
        self.finish_quit = MagicMock()
        self.quit_requested = MagicMock()
        self.status_banner = MagicMock()
        self._new_chat = MagicMock()
        self.toggle_window_visibility = MagicMock()
        self.toggle_history = MagicMock()
        self.worker = MagicMock()
        self.send_prompt = MagicMock()
        self.apply_preset = MagicMock()
        self.transcriber = MagicMock()
        self.settings_page = MagicMock()


class ShortcutTestCase(unittest.TestCase):
    """Build a shortcut manager whose input helper never starts."""

    def setUp(self) -> None:
        """Create the manager around a fake main window."""
        self.app = application()
        self.window = FakeMainWindow()
        self.addCleanup(self.window.close)
        self.screenshots = MagicMock()
        with patch.object(HookClient, "start"):
            self.manager = ShortcutManager(self.window, self.screenshots)  # type: ignore[arg-type]
        self.client = self.manager.hook_client
        KEPT.append((self.window, self.manager))
        screen = self.manager.screen_rect
        self.window.setGeometry(screen.left() + 100, screen.top() + 100, 300, 200)

    def finish_animation(self) -> None:
        """Run the move animation to its end."""
        for _ in range(100):
            if not self.manager.animation_active:
                return
            self.manager._animate_step()
        self.fail("The move animation never finished.")


class ActionTests(ShortcutTestCase):
    """Run each named action and check its effect."""

    def test_every_hotkey_action_exists(self) -> None:
        """Bind every hotkey name to a callable action."""
        self.assertEqual(set(self.manager.actions), set(ACTION_NAMES))

    def test_signal_actions_reach_the_main_window(self) -> None:
        """Run each signal-backed action and see it arrive at its handler."""
        window = self.window
        expectations = {
            "toggle_window_visibility": window.toggle_window_visibility,
            "new_chat": window._new_chat,
            "toggle_history": window.toggle_history,
            "stop_generation": window.worker.cancel,
            "toggle_recording": window.transcriber.toggle,
            "toggle_click_through": window.settings_page.click_through_checkbox.toggle,
            "dismiss_warnings": window.status_banner.dismiss_all,
        }
        for name, handler in expectations.items():
            with self.subTest(action=name):
                self.manager._run_action(name)
                handler.assert_called()

    def test_scrolling_moves_the_chat_by_a_step(self) -> None:
        """Scroll the chat up and down by one step."""
        self.manager._run_action("scroll_up")
        self.manager._run_action("scroll_down")
        self.assertEqual(
            [
                call.args
                for call in self.window.chat_area.shortcut_scroll.call_args_list
            ],
            [(-100,), (100,)],
        )

    def test_screenshot_and_answer(self) -> None:
        """Capture the screen Que is on, and send the interview prompt after it."""
        self.manager._run_action("screenshot")
        self.screenshots.take_screenshot.assert_called_with(int(self.window.winId()))
        self.manager._run_action("answer")
        self.assertEqual(self.screenshots.take_screenshot.call_count, 2)
        self.window.send_prompt.assert_called_once_with(
            INTERVIEW_LABEL, INTERVIEW_PROMPT
        )

    def test_presets_send_their_slot(self) -> None:
        """Apply preset N for each number shortcut."""
        for slot in range(1, PRESET_COUNT + 1):
            self.manager._run_action(f"preset_{slot}")
        self.assertEqual(
            [call.args[0] for call in self.window.apply_preset.call_args_list],
            list(range(1, PRESET_COUNT + 1)),
        )

    def test_unknown_actions_are_ignored(self) -> None:
        """Ignore an action name the helper might send from a newer version."""
        self.manager._run_action("not_an_action")

    def test_quit_waits_for_held_keys(self) -> None:
        """Quit at once only if no swallowed press is still held."""
        with patch.object(self.client, "request_quit", return_value=True):
            self.manager._run_action("close_app")
        self.window.finish_quit.assert_not_called()
        with patch.object(self.client, "request_quit", return_value=False):
            self.manager.request_quit()
        self.window.finish_quit.assert_called_once()


class AlertTests(ShortcutTestCase):
    """Make sure the user sees that input may have reached another application."""

    def test_alert_shows_the_hidden_overlay(self) -> None:
        """Show the overlay before warning, so the warning is seen."""
        self.manager._on_input_alert("Keys leaked")
        self.window.toggle_window_visibility.assert_called_once()
        self.window.status_banner.show_warning.assert_called_with(
            "Keys leaked", critical=True, lasting=True
        )

    def test_one_off_leak_is_not_lasting(self) -> None:
        """Show a one-off leak as a warning that does not stay until resolved."""
        self.window.show()
        self.client.leaked.emit("A key leaked")
        self.window.toggle_window_visibility.assert_not_called()
        self.window.status_banner.show_warning.assert_called_with(
            "A key leaked", critical=True, lasting=False
        )

    def test_visibility_is_published_to_the_helper(self) -> None:
        """Tell the helper when the overlay shows and hides, and ignore other events."""
        with patch.object(self.client, "set_visible") as visible:
            self.manager.eventFilter(self.window, QShowEvent())
            self.manager.eventFilter(self.window, QHideEvent())
            self.manager.eventFilter(self.window, QEvent(QEvent.Type.Move))
        self.assertEqual(
            [call.args for call in visible.call_args_list], [(True,), (False,)]
        )


class MoveAndResizeTests(ShortcutTestCase):
    """Move and resize the overlay by keyboard, inside the screen."""

    def test_moves_animate_to_one_step_in_each_direction(self) -> None:
        """Move one step per press in each direction, easing towards the target."""
        start = self.window.pos()
        steps = QPoint(
            self.manager.max_move_distance_x, self.manager.max_move_distance_y
        )
        for name, expected in (
            ("move_window_right", start + QPoint(steps.x(), 0)),
            ("move_window_down", start + steps),
            ("move_window_left", start + QPoint(0, steps.y())),
            ("move_window_up", start),
        ):
            with self.subTest(action=name):
                self.manager._run_action(name)
                self.manager._animate_step()
                if self.manager.animation_active:  # Part way, between start and target
                    self.assertNotEqual(self.window.pos(), expected)
                self.finish_animation()
                self.assertEqual(self.window.pos(), expected)

    def test_held_direction_keys_combine_before_the_first_frame(self) -> None:
        """Add moves that arrive before the first frame, and drop ones in the first half."""
        start = self.window.pos()
        self.manager._move_by_direction(1, 0)
        self.manager._move_by_direction(0, 1)  # Same frame: becomes diagonal
        target = self.manager.animation_target_pos
        self.manager._animate_step()
        self.manager._move_by_direction(1, 0)  # Mid-move: ignored
        self.assertEqual(self.manager.animation_target_pos, target)
        self.finish_animation()
        self.assertEqual(
            self.window.pos(),
            start
            + QPoint(
                self.manager.max_move_distance_x, self.manager.max_move_distance_y
            ),
        )

    def test_moves_stop_at_the_screen_edge(self) -> None:
        """Keep the overlay on screen however far it is moved."""
        screen = self.manager.screen_rect
        for _ in range(30):
            self.manager._move_by_direction(-1, -1)
            self.finish_animation()
        offset = self.manager.screen_bounds_offset
        self.assertEqual(
            self.window.pos(), QPoint(screen.left() + offset, screen.top() + offset)
        )

    def test_resizes_step_and_keep_a_minimum(self) -> None:
        """Resize by one step per press, never below 200 by 150."""
        self.manager._run_action("resize_window_wider")
        self.manager._run_action("resize_window_taller")
        self.app.processEvents()
        self.assertEqual((self.window.width(), self.window.height()), (340, 240))
        self.manager._run_action("resize_window_wider")
        self.app.processEvents()
        self.assertEqual(self.window.width(), 380)
        for _ in range(20):
            self.manager._run_action("resize_window_narrower")
            self.manager._run_action("resize_window_shorter")
        self.app.processEvents()
        self.assertEqual((self.window.width(), self.window.height()), (200, 150))

    def test_queued_resize_steps_apply_as_one(self) -> None:
        """Apply repeats that arrive while the window is busy in a single resize."""
        with patch.object(self.window, "resize", wraps=self.window.resize) as resize:
            for _ in range(3):
                self.manager._run_action("resize_window_wider")
            self.manager._run_action("resize_window_narrower")
            # Nothing applies until Qt is idle
            self.assertEqual(self.window.width(), 300)
            self.app.processEvents()
        resize.assert_called_once_with(380, 200)


if __name__ == "__main__":
    unittest.main()
