from PyQt6 import sip
from PyQt6.QtCore import QMimeData, Qt, QTimer, pyqtBoundSignal, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.input.input_settings import InputSettings
from ui.theme import BG_COLOR, PRIMARY_COLOR, qss


class InputField(QWidget):
    """Render the input bar and message field."""

    message_sent = pyqtSignal(str)
    answer_requested = pyqtSignal()
    model_changed = pyqtSignal(str)
    effort_changed = pyqtSignal(str)
    height_changed = pyqtSignal(int)

    def __init__(self, main_window: QWidget) -> None:
        super().__init__(main_window)
        self._init_UI()

    def _init_UI(self) -> None:
        """Initialize the input field UI layout and resizable text field."""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        root_layout = QHBoxLayout(self)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(0)

        # Grey rounded container that holds both the text edit and the settings row
        self.input_container = QWidget()
        self.input_container.setObjectName("inputContainer")
        self.input_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.input_container.setStyleSheet(f"""
            QWidget#inputContainer {{
                background-color: transparent;
                border: 1px solid {qss(PRIMARY_COLOR, 200)};
                border-radius: 14px;
            }}
        """)

        input_container_layout = QVBoxLayout(self.input_container)
        input_container_layout.setContentsMargins(
            8, 8, 8, 5
        )  # Smaller bottom margin due to vertically centered InputSettings row
        input_container_layout.setSpacing(0)

        self.input_field = _AutoResizeTextEdit()
        self.input_field.setPlaceholderText("How can I help you?")
        self.input_field.height_changed.connect(
            lambda delta: _emit_later(self, self.height_changed, delta)
        )
        self.input_field.send_requested.connect(self._send_message)

        font = QFont("Microsoft JhengHei", 10)
        self.input_field.setFont(font)

        # Text edit is transparent; the container provides the visual frame
        self.input_field.setStyleSheet(f"""
            QTextEdit {{
                background-color: transparent;
                color: {qss(PRIMARY_COLOR, 255)};
                border: none;
                padding: 2px 6px 2px 6px;  /* top, right, bottom, left */
            }}
        """)
        scrollbar = self.input_field.verticalScrollBar()
        assert scrollbar is not None
        scrollbar.setStyleSheet("""
            QScrollBar:vertical {
                background-color: transparent;
                width: 4px;
                margin: 2px 0px 2px 0px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background-color: rgba(255, 255, 255, 102);
                border-radius: 2px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: rgba(255, 255, 255, 128);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
                border: none;
                background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)

        self.input_settings = InputSettings(self.input_container)
        self.input_settings.model_changed.connect(self.model_changed)
        self.input_settings.effort_changed.connect(self.effort_changed)
        self.height_changed.connect(self.input_settings.reposition_popups)

        self.send_button = _SendButton(self.input_container)
        self.send_button.clicked.connect(
            lambda: self._send_message(answer_if_empty=True)
        )
        self.input_field.textChanged.connect(self._update_send_button)
        self.input_settings.models_changed.connect(self._update_send_button)
        self._update_send_button()

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self.input_settings, 1)
        footer.addWidget(self.send_button)
        input_container_layout.addWidget(self.input_field)
        input_container_layout.addLayout(footer)

        root_layout.addWidget(self.input_container)

    def _update_send_button(self) -> None:
        """Show the action available for the current draft and model choices."""
        has_message = bool(self.input_field.toPlainText().strip())
        can_send = bool(self.input_settings.models)
        label = "Send" if has_message else "Answer"
        self.send_button.setText(label)
        self.send_button.setAccessibleName(label)
        self.send_button.setEnabled(not has_message or can_send)
        if has_message:
            tooltip = (
                "Send message (Enter)"
                if can_send
                else "Load a model to send your message"
            )
        else:
            tooltip = "Answer from a screenshot (Caps Lock+C)"
        self.send_button.setToolTip(tooltip)

    def _send_message(self, *, answer_if_empty: bool = False) -> None:
        """Send the current message and clear the input field.

        Args:
            answer_if_empty (bool): Whether a blank draft runs the Answer shortcut.
        """
        message = self.input_field.toPlainText().strip()
        if message and self.input_settings.models:
            self.input_settings.close_popups()
            self.message_sent.emit(message)
            self.input_field.clear()
            self.input_field.setFocus()
        elif not message and answer_if_empty:
            self.answer_requested.emit()

    def _set_enabled(self, enabled: bool) -> None:
        """Enable or disable the input field.

        Args:
            enabled (bool): Whether the input field should accept user input.
        """
        self.input_field.setEnabled(enabled)


class _SendButton(QPushButton):
    """Display the current composer action and accept single clicks."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__("Answer", parent)
        self.setObjectName("sendButton")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        font = self.font()
        font.setPixelSize(12)
        font.setBold(True)
        self.setFont(font)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {qss(PRIMARY_COLOR)};
                border: 1px solid {qss(PRIMARY_COLOR, 200)};
                border-radius: 7px;
                padding: 0 9px;
            }}
            QPushButton:hover {{
                background: {qss(PRIMARY_COLOR)};
                color: {qss(BG_COLOR)};
            }}
            QPushButton:pressed {{
                background: {qss(BG_COLOR)};
                color: {qss(PRIMARY_COLOR)};
            }}
            QPushButton:disabled {{
                color: {qss(PRIMARY_COLOR, 128)};
                border-color: {qss(PRIMARY_COLOR, 96)};
            }}
        """)
        self.setFixedSize(max(62, self.sizeHint().width()), 24)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        """Consume the second press so a double-click sends a single request.

        Args:
            event (QMouseEvent): The second press of a double-click.
        """
        if event is not None:
            event.accept()


class _AutoResizeTextEdit(QTextEdit):
    """Resize a text edit automatically to fit its content."""

    height_changed = pyqtSignal(int)
    send_requested = pyqtSignal()

    MAX_LINES = 10

    def __init__(self) -> None:
        super().__init__()
        self._prev_height: int = 0
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        doc = self.document()
        assert doc is not None
        doc.contentsChanged.connect(self._adjust_height)
        QTimer.singleShot(0, self._adjust_height)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        """Send the message on Enter and insert a newline on Shift+Enter.

        Args:
            event (QKeyEvent): The key event being handled.
        """
        if (
            event is not None
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: QMimeData | None) -> None:
        """Override method to insert plain text only, stripping any rich text formatting from pasted content.

        Args:
            source (QMimeData): The mime data being pasted.
        """
        if source is not None and source.hasText():
            self.insertPlainText(source.text())

    def _adjust_height(self) -> None:
        """Adjust height based on document content, capped at MAX_LINES."""
        font_metrics = self.fontMetrics()
        line_height = font_metrics.lineSpacing()
        max_height = line_height * self.MAX_LINES

        doc = self.document()
        assert doc is not None
        doc_height = doc.size().height()
        margins = self.contentsMargins().top() + self.contentsMargins().bottom()
        desired_height = int(doc_height + margins)
        new_height = (
            max_height + margins if desired_height > max_height else desired_height
        )

        self.setFixedHeight(new_height)
        if self._prev_height != 0:
            delta = new_height - self._prev_height
            if delta != 0:
                _emit_later(self, self.height_changed, delta)
        self._prev_height = new_height


def _emit_later(widget: QWidget, signal: pyqtBoundSignal, value: int) -> None:
    """Emit a signal on the next event-loop pass unless its widget is deleted first.

    A queued lambda outlives the widget it captures, and emitting a deleted widget's
    signal crashes the process.

    Args:
        widget (QWidget): The widget that owns the signal.
        signal (pyqtBoundSignal): The signal to emit.
        value (int): The value to emit.
    """

    def emit() -> None:
        """Emit the signal if the widget still exists."""
        if not sip.isdeleted(widget):
            signal.emit(value)

    QTimer.singleShot(0, emit)
