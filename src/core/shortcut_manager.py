import math

from PyQt6.QtCore import QObject, QPoint, QTimer, pyqtSignal

from core.platform.keyboard_hook import (
    HotkeyEntry,
    HotkeyTable,
    Key,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    create_keyboard_hook,
)
from core.screenshot_manager import ScreenshotManager
from ui.window.main_window import MainWindow


class ShortcutManager(QObject):
    """Manage global keyboard shortcuts via a platform keyboard hook.

    The platform-specific hook backend (Win32 low-level hook on Windows, Quartz
    event tap on macOS) intercepts keystrokes and suppresses matched Que hotkey
    combinations so no other application sees them, while letting non-Que
    combinations reach other applications intact.

    Conditional hotkeys (everything except the visibility toggle) are only
    active while the main window is visible.
    """

    # Qt signals for thread-safe communication with the main thread
    # ALL UI actions must be performed on the main thread to avoid crashes and unpredictable behaviour (ex. leaking hotkeys)
    move_signal = pyqtSignal(int, int)
    scroll_signal = pyqtSignal(int)
    quit_signal = pyqtSignal()
    screenshot_signal = pyqtSignal()
    clear_all_chat_signal = pyqtSignal()
    minimize_signal = pyqtSignal()
    toggle_signal = pyqtSignal()
    send_message_signal = pyqtSignal(str)

    def __init__(
        self, main_window: MainWindow, screenshot_manager: ScreenshotManager
    ) -> None:
        super().__init__()
        self.main_window = main_window
        self.screenshot_manager = screenshot_manager

        # Connect signals
        self.move_signal.connect(
            self._start_animation
        )  # Signal needed because QTimers cannot be started from another thread
        self.scroll_signal.connect(self.main_window.chat_area.shortcut_scroll)
        self.quit_signal.connect(self.main_window.quit_app)
        self.screenshot_signal.connect(self.screenshot_manager.take_screenshot)
        self.clear_all_chat_signal.connect(self.main_window._clear_all_chat)
        self.minimize_signal.connect(self.main_window.hide)
        self.toggle_signal.connect(self.main_window.toggle_window_visibility)
        self.send_message_signal.connect(self.main_window.send_message)

        # Initialize animation
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self._animate_step)
        self.animation_active = False
        self.animation_start_pos: QPoint | None = None
        self.animation_target_pos: QPoint | None = None
        self.animation_progress = 0.0
        self._setup_movement_distances()

        # Hotkey lookup tables: (modifier_bitmask, key) -> (callback, repeat_callbacks)
        self.always_active_hotkeys: HotkeyTable = {}
        self.main_window_hotkeys: HotkeyTable = {}
        self._set_hotkeys()

        # Platform keyboard hook (Win32 low-level hook or Quartz event tap)
        self._keyboard_hook = create_keyboard_hook(self)
        self._keyboard_hook.start()

    def is_main_window_visible(self) -> bool:
        """Return whether the main window is currently visible.

        Returns:
            bool: True if the main window is visible.
        """
        return self.main_window.isVisible()

    def resolve_hotkey(self, modifiers: int, key: Key) -> HotkeyEntry | None:
        """Return the hotkey entry matching the modifier mask and key, or None.

        Looks up the always-active table first, then the main-window table only
        while the main window is visible. This gating is shared by every
        platform backend so the visibility rule is applied identically.

        Args:
            modifiers (int): Bitmask of currently held modifier keys.
            key (Key): The non-modifier key token pressed.

        Returns:
            HotkeyEntry: The (callback, repeat_allowed) tuple, or None if no match.
        """
        hotkey_key = (modifiers, key)
        entry = self.always_active_hotkeys.get(hotkey_key)
        if entry is None and self.is_main_window_visible():
            entry = self.main_window_hotkeys.get(hotkey_key)
        return entry

    def _set_hotkeys(self) -> None:
        """Populate the hotkey lookup tables.

        Default Shortcuts:
            Ctrl + E - Show / hide main window (always active)
            Ctrl + D - Generate AI output (trigger on release)
            Ctrl + G - Fix / improve code (trigger on release)
            Ctrl + Alt + <ArrowKeys> - Move main window
            Ctrl + Shift + Up / Down - Scroll chat area
            Ctrl + Shift + S - Take a screenshot
            Ctrl + N - Clear chat history
            Ctrl + Q - Minimize main window
            Ctrl + Alt + Shift + Q - Quit the application
        """
        self.always_active_hotkeys = {
            (MOD_CTRL, Key.E): (self._toggle_window_visibility, False),
            (MOD_CTRL | MOD_ALT | MOD_SHIFT, Key.Q): (self._close_app, False),
        }

        self.main_window_hotkeys = {
            (MOD_CTRL | MOD_ALT, Key.LEFT): (self._move_window_left, True),
            (MOD_CTRL | MOD_ALT, Key.RIGHT): (self._move_window_right, True),
            (MOD_CTRL | MOD_ALT, Key.UP): (self._move_window_up, True),
            (MOD_CTRL | MOD_ALT, Key.DOWN): (self._move_window_down, True),
            (MOD_CTRL | MOD_SHIFT, Key.UP): (self._scroll_up, True),
            (MOD_CTRL | MOD_SHIFT, Key.DOWN): (self._scroll_down, True),
            (MOD_CTRL | MOD_SHIFT, Key.S): (self._screenshot, False),
            (MOD_CTRL, Key.Q): (self._minimize, False),
            (MOD_CTRL, Key.N): (self._clear_all_chat, False),
            (MOD_CTRL, Key.D): (self._generate_with_screenshot, False),
            (MOD_CTRL, Key.G): (self._generate_with_screenshot_fix, False),
        }

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

    def _move_window_left(self) -> None:
        """Move the main window left."""
        if self.animation_active and self.animation_progress < 0.5:
            return
        new_x = max(
            self.screen_bounds_offset,
            self.main_window.geometry().x() - self.max_move_distance_x,
        )
        self.move_signal.emit(new_x, self.main_window.geometry().y())

    def _move_window_right(self) -> None:
        """Move the main window right."""
        if self.animation_active and self.animation_progress < 0.5:
            return
        max_x = (
            self.screen_rect.width()
            - self.main_window.geometry().width()
            - self.screen_bounds_offset
        )
        new_x = min(max_x, self.main_window.geometry().x() + self.max_move_distance_x)
        self.move_signal.emit(new_x, self.main_window.geometry().y())

    def _move_window_up(self) -> None:
        """Move the main window up."""
        if self.animation_active and self.animation_progress < 0.5:
            return
        new_y = max(
            self.screen_bounds_offset,
            self.main_window.geometry().y() - self.max_move_distance_y,
        )
        self.move_signal.emit(self.main_window.geometry().x(), new_y)

    def _move_window_down(self) -> None:
        """Move the main window down."""
        if self.animation_active and self.animation_progress < 0.5:
            return
        max_y = (
            self.screen_rect.height()
            - self.main_window.geometry().height()
            - self.screen_bounds_offset
        )
        new_y = min(max_y, self.main_window.geometry().y() + self.max_move_distance_y)
        self.move_signal.emit(self.main_window.geometry().x(), new_y)

    def _scroll_up(self) -> None:
        """Scroll up in the chat area."""
        self.scroll_signal.emit(-100)

    def _scroll_down(self) -> None:
        """Scroll down in the chat area."""
        self.scroll_signal.emit(100)

    def _close_app(self) -> None:
        """Close the application."""
        self.quit_signal.emit()  # Must emit signal to run on main thread

    def _screenshot(self) -> None:
        """Take a screenshot of the primary screen."""
        self.screenshot_signal.emit()

    def _minimize(self) -> None:
        """Minimize the main window."""
        self.minimize_signal.emit()

    def _clear_all_chat(self) -> None:
        """Clear the chat history."""
        self.clear_all_chat_signal.emit()

    def _generate_with_screenshot(self) -> None:
        """Take a screenshot then automatically generate content."""
        self.screenshot_manager.take_screenshot()
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

    def _generate_with_screenshot_fix(self) -> None:
        """Take a screenshot then automatically generate content to fix or improve code."""
        self.screenshot_manager.take_screenshot()
        self.send_message_signal.emit(
            "Fix or improve the code based on the new instructions. Then, state the changes you made."
        )
