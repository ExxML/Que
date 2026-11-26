"""Deliver owned physical input to Qt without activating any Que window."""

import ctypes
import time
from ctypes import wintypes
from typing import TypeGuard

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt, QTimer
from PyQt6.QtGui import (
    QContextMenuEvent,
    QEnterEvent,
    QFocusEvent,
    QHoverEvent,
    QKeyEvent,
    QMouseEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QPlainTextEdit,
    QTextEdit,
    QToolTip,
    QWidget,
)

from core.hook_engine import BUTTON_MESSAGES, QUE_KEY
from core.shortcut_manager import ShortcutManager
from core.win32_hook import VK_TO_MOD_BIT
from core.window_policy import native
from ui.chat.chat_bubble import ChatBubble

native.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
native.GetWindowRect.restype = wintypes.BOOL
native.GetForegroundWindow.restype = wintypes.HWND
native.GetKeyboardLayout.argtypes = [wintypes.DWORD]
native.GetKeyboardLayout.restype = wintypes.HANDLE
native.ToUnicodeEx.argtypes = [
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_ubyte),
    wintypes.LPWSTR,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.HANDLE,
]
native.ToUnicodeEx.restype = ctypes.c_int
native.GetTopWindow.argtypes = [wintypes.HWND]
native.GetTopWindow.restype = wintypes.HWND
native.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
native.GetWindow.restype = wintypes.HWND
native.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
native.ScreenToClient.restype = wintypes.BOOL
native.SendMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
native.SendMessageW.restype = wintypes.LPARAM

KEYS = {
    0x08: Qt.Key.Key_Backspace,
    0x09: Qt.Key.Key_Tab,
    0x0D: Qt.Key.Key_Return,
    0x1B: Qt.Key.Key_Escape,
    0x20: Qt.Key.Key_Space,
    0x21: Qt.Key.Key_PageUp,
    0x22: Qt.Key.Key_PageDown,
    0x23: Qt.Key.Key_End,
    0x24: Qt.Key.Key_Home,
    0x25: Qt.Key.Key_Left,
    0x26: Qt.Key.Key_Up,
    0x27: Qt.Key.Key_Right,
    0x28: Qt.Key.Key_Down,
    0x2D: Qt.Key.Key_Insert,
    0x2E: Qt.Key.Key_Delete,
    0x14: Qt.Key.Key_CapsLock,
}


class InputRouter(QObject):
    """Maintain local focus, pointer capture, hover, and layout-aware text delivery."""

    def __init__(
        self, application: QApplication, shortcut_manager: ShortcutManager
    ) -> None:
        super().__init__(application)
        self.application = application
        self.shortcut_manager = shortcut_manager
        self.hook_client = shortcut_manager.hook_client
        self.enabled = True
        self.regions: tuple[tuple[int, int, int, int, int], ...] = ()
        self.windows: dict[int, QWidget] = {}
        self.target: QWidget | None = None
        self.target_window_id = 0
        self.return_targets: dict[int, QWidget] = {}
        self.last_modal: QWidget | None = None
        self.hovered: QWidget | None = None
        self.pressed: dict[int, QWidget] = {}
        self.buttons = Qt.MouseButton.NoButton
        self.last_click = (0.0, 0, QPoint())
        self.last_position = QPoint()
        self.drag: tuple[QWidget, QPoint, QRect, str] | None = None
        self.drag_position: QPoint | None = None  # Latest move not yet applied
        self.frame_action: tuple[QWidget, int] | None = None
        self.translator = KeyTranslator()
        self.closed = False
        self.refresh_pending = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_regions)
        self.timer.start()
        self.tooltip_timer = QTimer(self)
        self.tooltip_timer.setSingleShot(True)
        self.tooltip_timer.setInterval(700)
        self.tooltip_timer.timeout.connect(self._show_tooltip)
        self.hook_client.pointer_input.connect(self.on_pointer)
        self.hook_client.key_input.connect(self.on_key)
        self.hook_client.armed_changed.connect(self.on_armed_changed)
        self.shortcut_manager.main_window.mouse_routing_changed.connect(
            self.set_enabled
        )
        self.set_enabled(
            not self.shortcut_manager.main_window.ai_sender.settings_store.click_through()
        )
        application.installEventFilter(self)
        application.aboutToQuit.connect(self.close)
        self.refresh_regions()

    def close(self) -> None:
        """Stop routing and detach filters before Qt destroys its native windows."""
        self.closed = True
        self.enabled = False
        self.regions = ()
        self.hook_client.set_routing(False)
        self.hook_client.set_regions([])
        self.timer.stop()
        self.tooltip_timer.stop()
        self.application.removeEventFilter(self)
        self.disarm()
        self.windows.clear()

    def set_enabled(self, enabled: bool) -> None:
        """Switch interaction routing while retaining permanent native click-through.

        Args:
            enabled (bool): Whether new pointer presses should interact with Que.
        """
        self.enabled = enabled
        self.hook_client.set_routing(enabled)
        if not enabled:
            self.disarm()

    def disarm(self, clear_returns: bool = True) -> None:
        """End local keyboard focus and discard pending dead-key composition.

        Args:
            clear_returns (bool): Whether to forget focus before transient windows opened.
        """
        self.shortcut_manager.arm_input(False)
        if self._alive(self.target):
            QApplication.sendEvent(self.target, QFocusEvent(QEvent.Type.FocusOut))
        self.target = None
        self.target_window_id = 0
        if clear_returns:
            self.return_targets.clear()
        self.translator.reset()

    def refresh_regions(self) -> None:
        """Publish native hit rectangles without exposing widgets to either low-level hook."""
        self.refresh_pending = False
        if self.closed:
            return
        previous_windows = set(self.windows)
        regions: dict[int, tuple[int, int, int, int, int]] = {}
        self.windows = {}
        for window in QApplication.topLevelWidgets():
            if not window.isVisible() or window.windowType() == Qt.WindowType.ToolTip:
                continue
            hwnd = int(window.winId())
            rect = wintypes.RECT()
            if native.GetWindowRect(hwnd, ctypes.byref(rect)):
                regions[hwnd] = (hwnd, rect.left, rect.top, rect.right, rect.bottom)
                self.windows[hwnd] = window
        ordered = []
        hwnd = native.GetTopWindow(None)
        seen: set[int] = set()
        while hwnd and hwnd not in seen:
            seen.add(hwnd)
            if hwnd in regions:
                ordered.append(regions.pop(hwnd))
            hwnd = native.GetWindow(hwnd, 2)
        self.regions = tuple(ordered)
        self.hook_client.set_regions(ordered)
        if self.target is not None and (
            not self._alive(self.target) or not self.target.isVisible()
        ):
            previous = self.return_targets.pop(self.target_window_id, None)
            self.disarm(False)
            if self._alive(previous) and previous.isVisible() and self.enabled:
                self._focus(previous)
        popup = self._top_popup()
        if (
            self.target is None
            and popup is not None
            and int(popup.winId()) not in previous_windows
            and self.enabled
        ):
            self._focus(popup.focusWidget() or popup)
        modal = QApplication.activeModalWidget()
        if (
            modal is not None
            and modal is not self.last_modal
            and self.enabled
            and (
                not self._alive(self.target)
                or self.target.window() is not modal
                and not modal.isAncestorOf(self.target)
            )
        ):
            self._focus(modal.focusWidget() or modal)
        self.last_modal = modal

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """Track window transitions and logical focus on the main Qt thread."""
        if event is not None and isinstance(watched, QWidget):
            if (
                event.type()
                in (
                    QEvent.Type.Show,
                    QEvent.Type.Hide,
                    QEvent.Type.Move,
                    QEvent.Type.Resize,
                )
                and not self.refresh_pending
            ):
                self.refresh_pending = True
                QTimer.singleShot(0, self.refresh_regions)
            if (
                event.type() == QEvent.Type.Hide
                and watched is self.shortcut_manager.main_window
            ):
                self.disarm()
                self._close_popups()
        return False

    def on_key(
        self,
        vk: int,
        scan: int,
        flags: int,
        down: bool,
        repeated: bool,
        held: tuple[int, ...],
    ) -> None:
        """Translate and dispatch a consumed keyboard event on the Qt thread.

        Args:
            vk (int): The Windows virtual key.
            scan (int): The hardware scan code.
            flags (int): The low-level key flags.
            down (bool): Whether this is a press.
            repeated (bool): Whether this press is an autorepeat.
            held (tuple[int, ...]): The physical modifier keys at interception.
        """
        target = self._top_popup() or self.target
        if not self._alive(target) or not target.isVisible():
            return
        if isinstance(target, QWidget) and target.property("quePopup"):
            target = target.focusWidget() or target
        event = self.translator.translate(vk, scan, flags, down, repeated, held)
        if event is None:
            return
        if down and vk == 0x1B:
            popup = self._top_popup()
            if popup is not None:
                popup.close()
            else:
                QApplication.sendEvent(target, event)
                self.disarm()
            return
        if down and vk == 0x09 and not isinstance(target, (QTextEdit, QPlainTextEdit)):
            candidate = (
                target.previousInFocusChain()
                if 0x10 in held or 0xA0 in held or 0xA1 in held
                else target.nextInFocusChain()
            )
            for _ in range(1000):
                if candidate is None:
                    break
                if (
                    candidate.isVisible()
                    and candidate.isEnabled()
                    and candidate.focusPolicy() & Qt.FocusPolicy.TabFocus
                ):
                    self._focus(candidate)
                    return
                candidate = (
                    candidate.previousInFocusChain()
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                    else candidate.nextInFocusChain()
                )
                if candidate is target:
                    break
        QApplication.sendEvent(target, event)

    def on_armed_changed(self, armed: bool) -> None:
        """Leave local editing when the input helper ends typing mode, e.g. for Alt+Tab.

        Args:
            armed (bool): Whether Que's input is still in typing mode.
        """
        if not armed:
            self.disarm()

    def on_pointer(self, message: int, x: int, y: int, data: int, hwnd: int) -> None:
        """Deliver pointer events to local widgets without native focus or mouse capture.

        Args:
            message (int): The native mouse message.
            x (int): The physical desktop x coordinate.
            y (int): The physical desktop y coordinate.
            data (int): The wheel delta or extra button identifier.
            hwnd (int): The hit or captured top-level handle, or zero outside Que.
        """
        window = self.windows.get(hwnd)
        modal = QApplication.activeModalWidget()
        if (
            modal is not None
            and self._alive(window)
            and window is not modal
            and not modal.isAncestorOf(window)
        ):
            return
        button_value, down = BUTTON_MESSAGES.get(message, (0, False))
        if button_value == 8:
            button_value = 8 if data >> 16 == 1 else 16
        target: QWidget | None = None
        global_position = self.last_position
        if self._alive(window):
            point = wintypes.POINT(x, y)
            if native.ScreenToClient(hwnd, ctypes.byref(point)):
                local = QPoint(
                    round(point.x / window.devicePixelRatioF()),
                    round(point.y / window.devicePixelRatioF()),
                )
                global_position = window.mapToGlobal(local)
                target = window.childAt(local) or window
        self.last_position = global_position
        if message == 0x200:
            if self.drag is not None:
                # Moves arrive faster than a resize repaints; apply only the newest one
                if self.drag_position is None:
                    QTimer.singleShot(0, self._apply_drag)
                self.drag_position = global_position
                return
            captured = next(iter(self.pressed.values()), None)
            if self._alive(captured):
                target = captured
            self._hover(target, global_position)
            if self._alive(target):
                self._mouse_event(
                    target,
                    QEvent.Type.MouseMove,
                    global_position,
                    Qt.MouseButton.NoButton,
                )
            return
        self.tooltip_timer.stop()
        QToolTip.hideText()
        if button_value:
            button = Qt.MouseButton(button_value)
            if down:
                self._close_popups(window)
                if not self._alive(target) or not target.isEnabled():
                    self.disarm()
                    return
                if (
                    button == Qt.MouseButton.LeftButton
                    and self.hook_client.held_modifiers == (QUE_KEY,)
                    and self._speak_from(target, global_position)
                ):
                    return
                self.buttons |= button
                self.pressed[button_value] = target
                self._focus(self._focus_target(target))
                if (
                    button == Qt.MouseButton.LeftButton
                    and self._alive(window)
                    and not window.rect().contains(
                        window.mapFromGlobal(global_position)
                    )
                ):
                    hit = int(
                        native.SendMessageW(
                            hwnd, 0x84, 0, (x & 0xFFFF) | ((y & 0xFFFF) << 16)
                        )
                    )
                    if hit in (8, 9, 20):
                        self.frame_action = (window, hit)
                        return
                    edge = {
                        10: "l",
                        11: "r",
                        12: "t",
                        13: "tl",
                        14: "tr",
                        15: "b",
                        16: "bl",
                        17: "br",
                    }.get(hit, "")
                    if hit == 2 or edge:
                        self.drag = (window, global_position, window.geometry(), edge)
                        return
                if button == Qt.MouseButton.LeftButton and self._start_drag(
                    target, global_position
                ):
                    return
                now = time.monotonic()
                last_time, last_button, last_pos = self.last_click
                double = (
                    last_button == button_value
                    and (now - last_time) * 1000 <= QApplication.doubleClickInterval()
                    and (global_position - last_pos).manhattanLength()
                    <= QApplication.startDragDistance()
                )
                self.last_click = (
                    0.0 if double else now,
                    button_value,
                    global_position,
                )
                self._mouse_event(
                    target,
                    QEvent.Type.MouseButtonDblClick
                    if double
                    else QEvent.Type.MouseButtonPress,
                    global_position,
                    button,
                )
            else:
                self.buttons &= ~button
                captured = self.pressed.pop(button_value, None)
                if self.frame_action is not None:
                    frame, hit = self.frame_action
                    self.frame_action = None
                    if (
                        self._alive(frame)
                        and int(
                            native.SendMessageW(
                                int(frame.winId()),
                                0x84,
                                0,
                                (x & 0xFFFF) | ((y & 0xFFFF) << 16),
                            )
                        )
                        == hit
                    ):
                        if hit == 20:
                            frame.close()
                        elif hit == 8:
                            frame.showMinimized()
                        elif frame.isMaximized():
                            frame.showNormal()
                        else:
                            frame.showMaximized()
                    return
                if self.drag is not None:
                    self._apply_drag()
                    self.drag = None
                    return
                if self._alive(captured):
                    self._mouse_event(
                        captured,
                        QEvent.Type.MouseButtonRelease,
                        global_position,
                        button,
                    )
                    if button == Qt.MouseButton.RightButton:
                        QApplication.sendEvent(
                            captured,
                            QContextMenuEvent(
                                QContextMenuEvent.Reason.Mouse,
                                captured.mapFromGlobal(global_position),
                                global_position,
                            ),
                        )
        elif self._alive(target) and message in (0x20A, 0x20E):
            self._wheel(target, global_position, message, data)
        self.refresh_regions()

    def _speak_from(self, widget: QWidget, global_position: QPoint) -> bool:
        """Read a reply aloud from the clicked line when Caps Lock alone is held.

        The click selects nothing and leaves typing focus where it was; its release finds no
        pressed widget and is dropped.

        Args:
            widget (QWidget): The deepest widget under the pointer.
            global_position (QPoint): The logical global position of the click.

        Returns:
            bool: Whether the click was on a reply and was used for speech.
        """
        candidate: QWidget | None = widget
        while candidate is not None and not (
            isinstance(candidate, ChatBubble) and not candidate.is_user
        ):
            candidate = candidate.parentWidget()
        if candidate is None:
            return False
        self.shortcut_manager.arm_input(self._alive(self.target))
        self.shortcut_manager.main_window.speak_from(candidate, global_position)
        return True

    def _alive(self, widget: QWidget | None) -> TypeGuard[QWidget]:
        """Check a retained widget before accessing its C++ object.

        Args:
            widget (QWidget): The possibly deleted widget.

        Returns:
            bool: Whether the widget still exists.
        """
        return widget is not None and not sip.isdeleted(widget)

    def _focus(self, widget: QWidget | None) -> None:
        """Arm local focus without transferring operating-system keyboard focus.

        Args:
            widget (QWidget): The widget that should receive owned keyboard events.
        """
        if widget is not self.target:
            window = widget.window() if widget is not None else None
            if (
                window is not None
                and (window.property("quePopup") or isinstance(window, QDialog))
                and self._alive(self.target)
                and self.target.window() is not window
            ):
                self.return_targets[int(window.winId())] = self.target
            self.disarm(widget is None)
            self.target = widget
            self.target_window_id = int(window.winId()) if window is not None else 0
            if widget is not None:
                widget.setFocus(Qt.FocusReason.MouseFocusReason)
                QApplication.sendEvent(widget, QFocusEvent(QEvent.Type.FocusIn))
        self.shortcut_manager.arm_input(widget is not None)

    def _focus_target(self, widget: QWidget) -> QWidget | None:
        """Find the closest keyboard-capable ancestor of a pointer target.

        Args:
            widget (QWidget): The deepest hit widget.

        Returns:
            QWidget: The local focus target, when present.
        """
        candidate: QWidget | None = widget
        while candidate is not None:
            if candidate.focusPolicy() != Qt.FocusPolicy.NoFocus:
                return candidate.focusProxy() or candidate
            candidate = candidate.parentWidget()
        return None

    def _top_popup(self) -> QWidget | None:
        """Find the frontmost visible popup in the native ordering.

        Returns:
            QWidget: The first popup, when present.
        """
        for hwnd, *_ in self.regions:
            window = self.windows.get(hwnd)
            if (
                self._alive(window)
                and window.isVisible()
                and window.property("quePopup")
            ):
                return window
        return None

    def _close_popups(self, except_window: QWidget | None = None) -> None:
        """Close popup families on an outside press or when the overlay hides.

        Args:
            except_window (QWidget): The popup currently receiving the press.
        """
        # isAncestorOf stops at window boundaries, so walk the owners of the pressed popup instead
        owners: list[QWidget] = []
        owner = except_window.parentWidget() if except_window is not None else None
        while owner is not None:
            owners.append(owner)
            owner = owner.parentWidget()
        for widget in QApplication.topLevelWidgets():
            if (
                widget is not except_window
                and widget.property("quePopup")
                and widget.isVisible()
            ):
                if any(widget is owner for owner in owners):
                    continue
                parent = widget.parentWidget()
                if isinstance(parent, QComboBox):
                    parent.hidePopup()
                else:
                    widget.close()

    def _modifiers(self) -> Qt.KeyboardModifier:
        """Convert physical modifiers to Qt modifiers for pointer selection.

        Returns:
            Qt.KeyboardModifier: The current hook-tracked modifiers.
        """
        return KeyTranslator.modifiers(self.hook_client.held_modifiers)

    def _mouse_event(
        self,
        widget: QWidget,
        kind: QEvent.Type,
        position: QPoint,
        button: Qt.MouseButton,
    ) -> None:
        """Send one synthetic Qt mouse event with local and global coordinates.

        Args:
            widget (QWidget): The event recipient.
            kind (QEvent.Type): The Qt mouse event type.
            position (QPoint): The logical global position.
            button (Qt.MouseButton): The changing button.
        """
        event = QMouseEvent(
            kind,
            QPointF(widget.mapFromGlobal(position)),
            QPointF(position),
            button,
            self.buttons,
            self._modifiers(),
        )
        QApplication.sendEvent(widget, event)

    def _wheel(
        self, widget: QWidget, position: QPoint, message: int, data: int
    ) -> None:
        """Offer a wheel event to the hit widget and then its ancestors until one accepts it.

        Qt propagates only spontaneous wheel events, so the routed event is offered
        to each ancestor here, as Qt would for physical input.

        Args:
            widget (QWidget): The deepest widget under the pointer.
            position (QPoint): The logical global position.
            message (int): The native vertical or horizontal wheel message.
            data (int): The native mouse data holding the wheel delta.
        """
        delta = ctypes.c_short(data >> 16).value
        angle = QPoint(
            delta if message == 0x20E else 0, delta if message == 0x20A else 0
        )
        receiver: QWidget | None = widget
        while self._alive(receiver):
            event = QWheelEvent(
                QPointF(receiver.mapFromGlobal(position)),
                QPointF(position),
                QPoint(),
                angle,
                self.buttons,
                self._modifiers(),
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            # Delivered accepted; Qt's default handler ignores it so the parent is offered it next
            QApplication.sendEvent(receiver, event)
            if event.isAccepted() or receiver.isWindow():
                return
            receiver = receiver.parentWidget()

    def _hover(self, widget: QWidget | None, position: QPoint) -> None:
        """Deliver hover transitions and schedule capture-excluded tooltips.

        Args:
            widget (QWidget): The current pointer target.
            position (QPoint): The logical global position.
        """
        if widget is self.hovered:
            return
        if self._alive(self.hovered):
            QApplication.sendEvent(self.hovered, QEvent(QEvent.Type.Leave))
            QApplication.sendEvent(
                self.hovered,
                QHoverEvent(QEvent.Type.HoverLeave, QPointF(-1, -1), QPointF(-1, -1)),
            )
        self.hovered = widget
        self.tooltip_timer.stop()
        QToolTip.hideText()
        if self._alive(widget):
            local = QPointF(widget.mapFromGlobal(position))
            QApplication.sendEvent(widget, QEnterEvent(local, local, QPointF(position)))
            QApplication.sendEvent(
                widget, QHoverEvent(QEvent.Type.HoverEnter, local, QPointF(-1, -1))
            )
            self.tooltip_timer.start()

    def _show_tooltip(self) -> None:
        """Show the hovered control's tooltip without relying on active-window state."""
        widget = self.hovered
        while self._alive(widget):
            if widget.toolTip():
                QToolTip.showText(self.last_position, widget.toolTip(), widget)
                return
            widget = widget.parentWidget()

    def _start_drag(self, widget: QWidget, position: QPoint) -> bool:
        """Start a local frame move or edge resize without native system commands.

        Args:
            widget (QWidget): The pointer target.
            position (QPoint): The logical desktop position.

        Returns:
            bool: Whether a move or resize began.
        """
        window = widget.window()
        if window is not self.shortcut_manager.main_window:
            return False
        local = window.mapFromGlobal(position)
        edge = (
            "l" if local.x() < 5 else "r" if local.x() >= window.width() - 5 else ""
        ) + ("t" if local.y() < 5 else "b" if local.y() >= window.height() - 5 else "")
        if edge or widget is window and local.y() < 32:
            self.drag = (window, position, window.geometry(), edge)
            return True
        return False

    def _apply_drag(self) -> None:
        """Apply the newest pending drag position, if any."""
        position, self.drag_position = self.drag_position, None
        if position is not None:
            self._drag_to(position)

    def _drag_to(self, position: QPoint) -> None:
        """Apply the current local move or resize on the Qt thread.

        Args:
            position (QPoint): The logical desktop position.
        """
        if self.drag is None:
            return
        widget, start, original, edge = self.drag
        delta = position - start
        rect = QRect(original)
        if not edge:
            rect.translate(delta)
        else:
            if "l" in edge:
                rect.setLeft(
                    min(
                        original.left() + delta.x(),
                        original.right() - max(200, widget.minimumWidth()),
                    )
                )
            if "r" in edge:
                rect.setRight(
                    max(
                        original.right() + delta.x(),
                        original.left() + max(200, widget.minimumWidth()),
                    )
                )
            if "t" in edge:
                rect.setTop(
                    min(
                        original.top() + delta.y(),
                        original.bottom() - max(150, widget.minimumHeight()),
                    )
                )
            if "b" in edge:
                rect.setBottom(
                    max(
                        original.bottom() + delta.y(),
                        original.top() + max(150, widget.minimumHeight()),
                    )
                )
        widget.setGeometry(rect)
        self.refresh_regions()


class KeyTranslator:
    """Translate keys using the foreground layout and a separate Qt-thread dead-key buffer."""

    def __init__(self) -> None:
        self.layout = 0
        self.dead_pending = False

    @staticmethod
    def modifiers(held: tuple[int, ...]) -> Qt.KeyboardModifier:
        """Convert a physical modifier snapshot to Qt flags.

        Args:
            held (tuple[int, ...]): The held modifier virtual keys.

        Returns:
            Qt.KeyboardModifier: The equivalent Qt modifier flags.
        """
        mask = 0
        for vk in held:
            mask |= VK_TO_MOD_BIT.get(vk, 0)
        result = Qt.KeyboardModifier.NoModifier
        for bit, flag in (
            (1, Qt.KeyboardModifier.ControlModifier),
            (2, Qt.KeyboardModifier.AltModifier),
            (4, Qt.KeyboardModifier.ShiftModifier),
        ):
            if mask & bit:
                result |= flag
        return result

    def reset(self) -> None:
        """Clear only this thread's pending dead key when local input is disarmed."""
        if self.layout:
            state = (ctypes.c_ubyte * 256)()
            output = ctypes.create_unicode_buffer(8)
            for _ in range(8):
                if (
                    native.ToUnicodeEx(
                        0x20, 0x39, state, output, len(output), 0, self.layout
                    )
                    >= 0
                ):
                    break
        self.dead_pending = False

    def translate(
        self,
        vk: int,
        scan: int,
        flags: int,
        down: bool,
        repeated: bool,
        held: tuple[int, ...],
    ) -> QKeyEvent | None:
        """Translate a physical key with foreground layout, dead keys, and AltGr.

        Args:
            vk (int): The virtual key code.
            scan (int): The hardware scan code.
            flags (int): The physical extended-key flags.
            down (bool): Whether the key is pressed.
            repeated (bool): Whether this is an autorepeat.
            held (tuple[int, ...]): The physical modifier snapshot.

        Returns:
            QKeyEvent | None: The translated event, or None for a pending dead key.
        """
        thread = native.GetWindowThreadProcessId(native.GetForegroundWindow(), None)
        layout = int(native.GetKeyboardLayout(thread) or 0)
        if layout != self.layout:
            self.reset()
            self.layout = layout
        if vk == 0x08 and down and self.dead_pending:
            self.reset()
            return None
        state = (ctypes.c_ubyte * 256)()
        for modifier in held:
            state[modifier] = 0x80
            generic = {1: 0x11, 2: 0x12, 4: 0x10}.get(VK_TO_MOD_BIT.get(modifier, 0))
            if generic is not None:
                state[generic] = 0x80
        if 0xA5 in held:
            state[0x11] = (
                0x80  # AltGr translation also needs Windows' synthetic Ctrl state.
            )
        state[vk] |= 0x80 if down else 0
        modifiers = self.modifiers(held)
        text = ""
        if down and (vk not in KEYS or vk == 0x20) and not 0x70 <= vk <= 0x87:
            output = ctypes.create_unicode_buffer(16)
            count = int(
                native.ToUnicodeEx(vk, scan, state, output, len(output), 0, self.layout)
            )
            if count < 0:
                self.dead_pending = True
                return None
            if count:
                self.dead_pending = False
                text = ctypes.string_at(ctypes.addressof(output), count * 2).decode(
                    "utf-16-le", errors="replace"
                )
                if 0xA5 in held:
                    modifiers &= ~(
                        Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.AltModifier
                    )
                elif modifiers & Qt.KeyboardModifier.ControlModifier:
                    text = ""
        key = int(KEYS.get(vk, Qt.Key.Key_unknown))
        if 0x30 <= vk <= 0x5A:
            key = vk
        elif 0x70 <= vk <= 0x87:
            key = int(Qt.Key.Key_F1) + vk - 0x70
        elif text:
            upper = text.upper()
            key = ord(upper) if len(upper) == 1 else int(Qt.Key.Key_unknown)
        if vk == 0x20 and not down:
            text = " "
        if 0x60 <= vk <= 0x6F or vk == 0x0D and flags & 1:
            modifiers |= Qt.KeyboardModifier.KeypadModifier
        return QKeyEvent(
            QEvent.Type.KeyPress if down else QEvent.Type.KeyRelease,
            key,
            modifiers,
            scan,
            vk,
            flags,
            text,
            repeated,
        )
