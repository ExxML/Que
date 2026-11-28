import math
from collections.abc import Callable
from functools import partial

from PyQt6.QtCore import QEvent, QObject, QPoint, QTimer, pyqtSignal

from core.hook_client import HookClient
from core.hook_engine import ACTION_NAMES, PRESET_COUNT
from core.prompts import INTERVIEW_LABEL, INTERVIEW_PROMPT
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
    screenshot_signal = pyqtSignal(int)
    new_chat_signal = pyqtSignal()
    toggle_signal = pyqtSignal()
    toggle_history_signal = pyqtSignal()
    stop_generation_signal = pyqtSignal()
    prompt_signal = pyqtSignal(str, str)
    preset_signal = pyqtSignal(int)
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
        # Read once: changing it mid-session would prompt or restart the helper, so it waits for the next launch
        self.hook_client = HookClient(
            elevate=main_window.ai_sender.settings_store.elevate_helper()
        )
        presets = {
            f"preset_{slot}": partial(self.preset_signal.emit, slot)
            for slot in range(1, PRESET_COUNT + 1)
        }
        self.actions: dict[str, Callable[[], None]] = {
            name: presets.get(name) or getattr(self, f"_{name}")
            for name in ACTION_NAMES
        }

        # Connect signals
        self.move_signal.connect(
            self._start_animation
        )  # Signal needed because QTimers cannot be started from another thread
        self.scroll_signal.connect(self.main_window.shortcut_scroll)
        self.quit_signal.connect(self.main_window.finish_quit)
        self.main_window.quit_requested.connect(self.request_quit)
        self.main_window.input_field.answer_requested.connect(self._answer)
        self.hook_failed.connect(self.main_window.status_banner.show_warning)
        self.move_direction.connect(self._move_by_direction)
        self.screenshot_signal.connect(self.screenshot_manager.take_screenshot)
        self.new_chat_signal.connect(self.main_window._new_chat)
        self.toggle_signal.connect(self.main_window.toggle_window_visibility)
        self.toggle_history_signal.connect(self.main_window.toggle_history)
        self.stop_generation_signal.connect(self.main_window.worker.cancel)
        self.prompt_signal.connect(self.main_window.send_prompt)
        self.preset_signal.connect(self.main_window.apply_preset)
        self.toggle_recording_signal.connect(self.main_window.transcriber.toggle)
        # Toggling the settings checkbox saves the change and applies it to the window
        self.toggle_click_through_signal.connect(
            self.main_window.settings_page.click_through_checkbox.toggle
        )
        self.hook_client.action.connect(self._run_action)
        self.hook_client.quit_ready.connect(self.quit_signal)
        self.hook_client.failed.connect(self.hook_failed)
        self.hook_client.alert.connect(self._on_input_alert)
        self.hook_client.leaked.connect(
            lambda message: self._on_input_alert(message, lasting=False)
        )
        self.hook_client.resolved.connect(self.main_window.status_banner.clear_warning)

        # Initialize animation
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self._animate_step)
        self.animation_active = False
        self.animation_start_pos: QPoint | None = None
        self.animation_target_pos: QPoint | None = None
        self.animation_progress = 0.0
        self.move_dx = 0  # Summed direction of the move being animated
        self.move_dy = 0
        # Summed directions of the resize steps waiting to be applied
        self.resize_dx = 0
        self.resize_dy = 0
        self.resize_pending = False
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

    def _on_input_alert(self, message: str, lasting: bool = True) -> None:
        """Make sure the user sees that input may have reached another application.

        Args:
            message (str): The warning.
            lasting (bool): Whether protection stays off until resolved, rather than a one-off leak.
        """
        if not self.main_window.isVisible():
            self.main_window.toggle_window_visibility()
        self.main_window.status_banner.show_warning(
            message, critical=True, lasting=lasting
        )

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
        self.resize_step = 40  # Pixels per resize shortcut press or repeat

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
        if self.animation_active and self.animation_progress == 0.0:
            # Held direction keys repeat together, so moves before the first frame add into one
            dx += self.move_dx
            dy += self.move_dy
        elif self.animation_active and self.animation_progress < 0.5:
            return
        self.move_dx, self.move_dy = dx, dy
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

    def _resize_by(self, dx: int, dy: int) -> None:
        """Queue a resize step, applied together with any other steps already waiting.

        Each step rewraps the whole chat, which in a long chat takes longer than a held key
        takes to repeat, so applying every repeat separately froze the window until it caught up.

        Args:
            dx (int): The width direction.
            dy (int): The height direction.
        """
        self.resize_dx += dx
        self.resize_dy += dy
        if not self.resize_pending:
            self.resize_pending = True
            # Runs after the repeats that arrived during the previous resize, which then join this one
            QTimer.singleShot(0, self._apply_resize)

    def _apply_resize(self) -> None:
        """Resize the main window by the waiting steps, keeping its top-left corner in place."""
        dx, dy = self.resize_dx, self.resize_dy
        self.resize_dx = self.resize_dy = 0
        self.resize_pending = False
        geometry = self.main_window.geometry()
        # Same minimum as edge-drag resizing; never shrink a window already past the screen edge
        max_width = self.screen_rect.right() - geometry.x() - self.screen_bounds_offset
        max_height = (
            self.screen_rect.bottom() - geometry.y() - self.screen_bounds_offset
        )
        width = max(
            200,
            self.main_window.minimumWidth(),
            min(
                max(geometry.width(), max_width),
                geometry.width() + dx * self.resize_step,
            ),
        )
        height = max(
            150,
            self.main_window.minimumHeight(),
            min(
                max(geometry.height(), max_height),
                geometry.height() + dy * self.resize_step,
            ),
        )
        self.main_window.resize(width, height)

    def _resize_window_narrower(self) -> None:
        """Make the main window narrower."""
        self._resize_by(-1, 0)

    def _resize_window_wider(self) -> None:
        """Make the main window wider."""
        self._resize_by(1, 0)

    def _resize_window_taller(self) -> None:
        """Make the main window taller."""
        self._resize_by(0, 1)

    def _resize_window_shorter(self) -> None:
        """Make the main window shorter."""
        self._resize_by(0, -1)

    def _scroll_up(self) -> None:
        """Scroll up in the open page."""
        self.scroll_signal.emit(-100)

    def _scroll_down(self) -> None:
        """Scroll down in the open page."""
        self.scroll_signal.emit(100)

    def _close_app(self) -> None:
        """Close the application."""
        self.request_quit()

    def _screenshot(self) -> None:
        """Take a screenshot of the screen Que is on."""
        self.screenshot_signal.emit(int(self.main_window.winId()))

    def _new_chat(self) -> None:
        """Start a new chat."""
        self.new_chat_signal.emit()

    def _toggle_history(self) -> None:
        """Show or hide the conversation history sidebar."""
        self.toggle_history_signal.emit()

    def _stop_generation(self) -> None:
        """Stop the reply being generated, keeping what has streamed so far."""
        self.stop_generation_signal.emit()

    def _toggle_recording(self) -> None:
        """Start or stop recording and transcribing audio."""
        self.toggle_recording_signal.emit()

    def _toggle_click_through(self) -> None:
        """Turn mouse click-through on or off for the main window."""
        self.toggle_click_through_signal.emit()

    def _answer(self) -> None:
        """Take a screenshot then answer the interview question, whatever kind it is."""
        self._screenshot()
        self.prompt_signal.emit(INTERVIEW_LABEL, INTERVIEW_PROMPT)

    def _speak_latest(self) -> None:
        """Read the newest reply aloud from the start."""
        self.main_window.speak_latest()

    def _toggle_speech(self) -> None:
        """Pause or resume reading aloud."""
        self.main_window.speaker.toggle_pause()

    def _rewind_speech(self) -> None:
        """Restart the sentence being read, or go further back when pressed again."""
        self.main_window.speaker.rewind()

    def _skip_speech(self) -> None:
        """Skip to the next sentence being read."""
        self.main_window.speaker.skip()

    def _dismiss_warnings(self) -> None:
        """Dismiss the visible protection warnings on the Qt thread."""
        self.main_window.status_banner.dismiss_all()
