import math
from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, pyqtSignal

from core.hook_client import HookClient
from core.hook_engine import ACTION_NAMES
from core.screenshot_manager import ScreenshotManager
from ui.window.main_window import MainWindow


class ShortcutManager(QObject):
    """Run Que's global shortcuts, whose physical keys are owned by the input helper process.

    The helper (see `core.hook_engine`) consumes Que's shortcut chords in full and
    replays every other chord intact, so other applications never see part of a Que
    shortcut. This class performs the actions on the Qt thread.

    Conditional hotkeys (everything except the visibility toggle, recording and quit)
    are only active while the main window is visible.
    """

    # Qt signals for thread-safe communication with the main thread
    # ALL UI actions must be performed on the main thread to avoid crashes and unpredictable behaviour
    move_signal = pyqtSignal(int, int)
    scroll_signal = pyqtSignal(int)
    quit_signal = pyqtSignal()
    screenshot_signal = pyqtSignal()
    clear_all_chat_signal = pyqtSignal()
    minimize_signal = pyqtSignal()
    toggle_signal = pyqtSignal()
    toggle_history_signal = pyqtSignal()
    send_message_signal = pyqtSignal(str)
    toggle_recording_signal = pyqtSignal()
    toggle_click_through_signal = pyqtSignal()
    hook_failed = pyqtSignal(str)
    move_direction = pyqtSignal(int, int)

    def __init__(
        self, main_window: MainWindow, screenshot_manager: ScreenshotManager
    ) -> None:
        super().__init__()
        self.main_window = main_window
        self.screenshot_manager = screenshot_manager
        self.hook_client = HookClient()
        self.actions: dict[str, Callable[[], None]] = {
            name: getattr(self, f"_{name}") for name in ACTION_NAMES
        }

        # Connect signals
        self.move_signal.connect(
            self._start_animation
        )  # Signal needed because QTimers cannot be started from another thread
        self.scroll_signal.connect(self.main_window.chat_area.shortcut_scroll)
        self.quit_signal.connect(self.main_window.finish_quit)
        self.main_window.quit_requested.connect(self.request_quit)
        self.hook_failed.connect(self.main_window.chat_area.show_stream_error)
        self.move_direction.connect(self._move_by_direction)
        self.screenshot_signal.connect(self.screenshot_manager.take_screenshot)
        self.clear_all_chat_signal.connect(self.main_window._new_chat)
        self.minimize_signal.connect(self.main_window.hide)
        self.toggle_signal.connect(self.main_window.toggle_window_visibility)
        self.toggle_history_signal.connect(self.main_window.toggle_history)
        self.send_message_signal.connect(self.main_window.send_message)
        self.toggle_recording_signal.connect(self.main_window.transcriber.toggle)
        # Toggling the settings checkbox saves the change and applies it to the window
        self.toggle_click_through_signal.connect(
            self.main_window.settings_page.click_through_checkbox.toggle
        )
        self.hook_client.action.connect(self._run_action)
        self.hook_client.quit_ready.connect(self.quit_signal)
        self.hook_client.failed.connect(self.hook_failed)
        self.hook_client.alert.connect(self._on_input_alert)

        # Initialize animation
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self._animate_step)
        self.animation_active = False
        self.animation_start_pos: QPoint | None = None
        self.animation_target_pos: QPoint | None = None
        self.animation_progress = 0.0
        self._setup_movement_distances()

        self.main_window.installEventFilter(self)
        self.hook_client.set_visible(self.main_window.isVisible())
        self.hook_client.start()

    def request_quit(self) -> None:
        """Quit once the input helper reports every swallowed physical press released."""
        if not self.hook_client.request_quit():
            self.quit_signal.emit()

    def arm_input(self, armed: bool) -> None:
        """Tell the input helper whether ordinary keys now belong to Que's input.

        Args:
            armed (bool): Whether Que's input is in typing mode.
        """
        self.hook_client.send_armed(armed)

    def stop(self) -> None:
        """Disconnect from the input helper as the application exits."""
        self.hook_client.stop()

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """Publish visibility to the input helper as the main window shows and hides."""
        if (
            watched is self.main_window
            and event is not None
            and event.type() in (QEvent.Type.Show, QEvent.Type.Hide)
        ):
            self.hook_client.set_visible(event.type() == QEvent.Type.Show)
        return False

    def _run_action(self, name: str) -> None:
        """Run a shortcut action the input helper matched.

        Args:
            name (str): The action name.
        """
        action = self.actions.get(name)
        if action is not None:
            action()

    def _on_input_alert(self, message: str) -> None:
        """Make sure the user sees that input may have reached another application.

        Args:
            message (str): The warning.
        """
        if not self.main_window.isVisible():
            self.main_window.toggle_window_visibility()
        self.main_window.chat_area.show_stream_error(message)

    # Hotkey Callback Functions

    def _setup_movement_distances(self) -> None:
        """Determine screen geometry and movement distances."""
        assert self.main_window.primary_screen is not None
        self.screen_rect = self.main_window.primary_screen.availableGeometry()
        self.max_move_distance_x = self.screen_rect.width() // 14
        self.max_move_distance_y = self.screen_rect.height() // 14
        self.screen_bounds_offset = (
            2  # Always keep 2 pixels to screen edge to prevent setGeometry errors
        )
        self.animation_duration = 100  # Animation duration in milliseconds
        self.animation_fps = 120  # Frames per second
        self.animation_frame_time = 1000 // self.animation_fps  # Time per frame in ms

    def _start_animation(self, target_x: int, target_y: int) -> None:
        """Begin an animated move to the target position.

        Args:
            target_x (int): Target x-coordinate for the main window.
            target_y (int): Target y-coordinate for the main window.
        """
        self.animation_start_pos = self.main_window.pos()
        self.animation_target_pos = QPoint(target_x, target_y)
        self.animation_progress = 0.0
        self.animation_active = True
        self.animation_timer.start(self.animation_frame_time)

    def _animate_step(self) -> None:
        """Advance one frame of the movement animation."""
        assert self.animation_start_pos is not None
        assert self.animation_target_pos is not None
        self.animation_progress += self.animation_frame_time / self.animation_duration

        if self.animation_progress >= 1.0:
            # Animation complete
            self.main_window.move(self.animation_target_pos)
            self.animation_timer.stop()
            self.animation_active = False
        else:
            # Ease-out sine motion: sin(t * π/2)
            ease_progress = math.sin(self.animation_progress * math.pi / 2)
            current_x = int(
                self.animation_start_pos.x()
                + (self.animation_target_pos.x() - self.animation_start_pos.x())
                * ease_progress
            )
            current_y = int(
                self.animation_start_pos.y()
                + (self.animation_target_pos.y() - self.animation_start_pos.y())
                * ease_progress
            )
            self.main_window.move(current_x, current_y)

    def _toggle_window_visibility(self) -> None:
        """Toggle the main window visibility."""
        self.toggle_signal.emit()

    def _move_by_direction(self, dx: int, dy: int) -> None:
        """Calculate and start a shortcut move on the Qt thread.

        Args:
            dx (int): The horizontal direction.
            dy (int): The vertical direction.
        """
        if self.animation_active and self.animation_progress < 0.5:
            return
        geometry = self.main_window.geometry()
        x = max(
            self.screen_rect.left() + self.screen_bounds_offset,
            min(
                self.screen_rect.right() - geometry.width() - self.screen_bounds_offset,
                geometry.x() + dx * self.max_move_distance_x,
            ),
        )
        y = max(
            self.screen_rect.top() + self.screen_bounds_offset,
            min(
                self.screen_rect.bottom()
                - geometry.height()
                - self.screen_bounds_offset,
                geometry.y() + dy * self.max_move_distance_y,
            ),
        )
        self._start_animation(x, y)

    def _move_window_left(self) -> None:
        """Request a move left on the Qt thread."""
        self.move_direction.emit(-1, 0)

    def _move_window_right(self) -> None:
        """Request a move right on the Qt thread."""
        self.move_direction.emit(1, 0)

    def _move_window_up(self) -> None:
        """Request a move up on the Qt thread."""
        self.move_direction.emit(0, -1)

    def _move_window_down(self) -> None:
        """Request a move down on the Qt thread."""
        self.move_direction.emit(0, 1)

    def _scroll_up(self) -> None:
        """Scroll up in the chat area."""
        self.scroll_signal.emit(-100)

    def _scroll_down(self) -> None:
        """Scroll down in the chat area."""
        self.scroll_signal.emit(100)

    def _close_app(self) -> None:
        """Close the application."""
        self.request_quit()

    def _screenshot(self) -> None:
        """Take a screenshot of the primary screen."""
        self.screenshot_signal.emit()

    def _minimize(self) -> None:
        """Minimize the main window."""
        self.minimize_signal.emit()

    def _clear_all_chat(self) -> None:
        """Clear the chat history."""
        self.clear_all_chat_signal.emit()

    def _toggle_history(self) -> None:
        """Show or hide the conversation history sidebar."""
        self.toggle_history_signal.emit()

    def _toggle_recording(self) -> None:
        """Start or stop recording and transcribing audio."""
        self.toggle_recording_signal.emit()

    def _toggle_click_through(self) -> None:
        """Turn mouse click-through on or off for the main window."""
        self.toggle_click_through_signal.emit()

    def _solve_coding_problem(self) -> None:
        """Take a screenshot then ask for a full solution to the coding problem on screen."""
        self.screenshot_signal.emit()
        self.send_message_signal.emit(
            """Help me solve this programming problem. Be concise.
1. Give me 5 clarification questions to ask about the problem.
2. State the type of problem (ex. Arrays & Hashing, Two Pointers, Sliding Window, Stack, Binary Search, Linked List, Trees, Heap / Priority Queue, Backtracking, Tries, Graphs, Advanced Graphs, 1-D Dynamic Programming, 2-D Dynamic Programming, Greedy, Intervals, Math & Geometry, and/or Bit Manipulation), data structure(s), what each element in the data structure means, and algorithm(s) used to solve this problem.
3. Give me a high-level, point-form plan to approach and solve this problem (including edge cases).
4. Give me a short example walkthrough using your solution.
5. Give me a code block with the solution in Python, supplied with comments.
6. Give me a concise explanation of the solution.
7. Give me the time and space complexity for the solution."""
        )

    def _fix_code(self) -> None:
        """Take a screenshot then ask to fix or improve the code on screen."""
        self.screenshot_signal.emit()
        self.send_message_signal.emit(
            "Fix or improve the code based on the new instructions. Then, state the changes you made."
        )

    def _solve_question(self) -> None:
        """Take a screenshot then ask for an answer and explanation to any question on screen."""
        self.screenshot_signal.emit()
        self.send_message_signal.emit(
            """Solve the question in the screenshot. It may be on any topic (ex. behavioural, data structures and algorithms, systems, databases, system design). Be concise.
1. Give the answer or solution first, in the format that suits the question (ex. code for a coding question, a query for a database question, a structured answer for a behavioural question, components and trade-offs for a system design question).
2. Then explain the reasoning behind it."""
        )
