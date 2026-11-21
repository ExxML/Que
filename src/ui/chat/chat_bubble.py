import math

from PyQt6.QtCore import QEvent, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QEnterEvent,
    QFont,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.chat.ai_formatter import format_message
from ui.theme import BG_COLOR, PRIMARY_COLOR, qcolor, qss

PREVIEW_WIDTH = 64
PREVIEW_HEIGHT = 36
PREVIEW_SPACING = 4


class ChatBubble(QWidget):
    """Render a chat bubble for displaying messages and what was attached to them."""

    screenshot_opened = pyqtSignal(list, int)  # Screenshot paths, index to show

    def __init__(
        self,
        message: str,
        is_user: bool = False,
        transcript: str = "",
        screenshots: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.message = message
        self.is_user = is_user
        self.transcript = transcript
        self.screenshots = list(screenshots)
        self._loading_timer: QTimer | None = None
        self._init_UI()

    def set_bot_message(self, message: str) -> None:
        """Format the bot message and set the text as the label.

        Args:
            message (str): The raw bot message text to format and display.
        """
        self.message = message
        self.message_label.setText(format_message(message))

    def mark_stopped(self) -> None:
        """Show a muted note after the message that the reply was stopped before it finished."""
        note = f"<div style='color: {qss(PRIMARY_COLOR, 140)}; font-style: italic;'>Stopped.</div>"
        self.message_label.setText(
            f"{format_message(self.message)}{note}" if self.message else note
        )

    def start_loading_animation(self) -> None:
        """Start the animated three-dot loading indicator."""
        self._loading_frame = 0
        self._loading_timer = QTimer(self)
        self._loading_timer.timeout.connect(self._tick_loading)
        self._loading_timer.start(80)
        self._tick_loading()

    def stop_loading_animation(self) -> None:
        """Stop the loading animation and clean up the timer."""
        if hasattr(self, "_loading_timer") and self._loading_timer is not None:
            self._loading_timer.stop()
            self._loading_timer.deleteLater()
            self._loading_timer = None

    def _init_UI(self) -> None:
        """Initialize the chat bubble UI layout and styling."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)

        # Create message label with HTML formatting
        # Default formatting for user message only (bot message uses formatting in set_bot_message)
        html_message = f'<div style="line-height: 1.4; white-space: pre-wrap;">{self.message}</div>'
        self.message_label = QLabel(html_message)
        self.message_label.setTextFormat(Qt.TextFormat.RichText)
        self.message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        # Set font
        font = QFont("Helvetica", 11)
        self.message_label.setFont(font)

        # Style the bubble based on sender
        if self.is_user:
            # User messages: light gray, aligned right
            self.message_label.setStyleSheet(f"""
                QLabel {{
                    color: {qss(PRIMARY_COLOR, 255)};
                    background-color: transparent;
                    border: 1px solid {qss(PRIMARY_COLOR, 255)};
                    border-radius: 8px;
                    padding: 5px 4px -3px 5px;  /* top, right, bottom, left - negative bottom padding to account for line spacing */
                }}
            """)  # padding adjusted to visually center text within the bubble

            # Let Qt estimate the natural width of the message, then word wrap if necessary
            natural_width = self.message_label.sizeHint().width()
            max_width = 400

            if natural_width > max_width:
                self.message_label.setFixedWidth(max_width)
                self.message_label.setWordWrap(True)
            else:
                self.message_label.setFixedWidth(natural_width)

            layout.addStretch()
            if self.transcript or self.screenshots:
                layout.addLayout(self._attachment_column(max_width))
            else:
                layout.addWidget(self.message_label)
        else:
            # Bot messages: transparent, aligned left
            self.message_label.setStyleSheet(f"""
                QLabel {{
                    color: {qss(PRIMARY_COLOR, 255)};
                    background-color: transparent;
                    padding: 0px 0px 0px 1px;  /* top, right, bottom, left */
                }}
            """)
            self.message_label.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
            )
            self.message_label.setWordWrap(True)
            self.message_label.setFixedWidth(515)
            layout.addWidget(self.message_label)
            layout.addStretch()

        layout.setSpacing(0)

    def _attachment_column(self, max_width: int) -> QVBoxLayout:
        """Stack the message between its screenshots above and a collapsed audio transcript disclosure below.

        Args:
            max_width (int): The widest the attachments may grow.

        Returns:
            QVBoxLayout: The column holding the message and its attachments.
        """
        column = QVBoxLayout()
        column.setSpacing(2)
        if self.screenshots:
            column.addLayout(self._screenshot_row(max_width))
            # Match the gap between previews
            column.addSpacing(PREVIEW_SPACING - column.spacing())
        column.addWidget(self.message_label, alignment=Qt.AlignmentFlag.AlignRight)
        if self.transcript:
            self._add_transcript(column, max_width)
        return column

    def _screenshot_row(self, max_width: int) -> QHBoxLayout:
        """Build a right-aligned row of screenshot previews that open the viewer.

        When more screenshots were sent than fit, the last preview counts the rest.

        Args:
            max_width (int): The widest the row may grow.

        Returns:
            QHBoxLayout: The row of previews.
        """
        row = QHBoxLayout()
        row.setSpacing(PREVIEW_SPACING)
        row.addStretch()
        total = len(self.screenshots)
        shown = min(total, max(1, max_width // (PREVIEW_WIDTH + PREVIEW_SPACING)))
        for index in range(shown):
            hidden = total - shown if index == shown - 1 else 0
            preview = _ScreenshotPreview(self.screenshots[index], hidden, self)
            preview.setAccessibleName(
                f"View screenshot {index + 1} of {total} sent with this message"
            )
            preview.setToolTip(
                f"View {total} screenshots" if hidden else "View screenshot"
            )
            preview.clicked.connect(
                lambda _checked=False, index=index: self.screenshot_opened.emit(
                    self.screenshots, index
                )
            )
            row.addWidget(preview)
        return row

    def _add_transcript(self, column: QVBoxLayout, max_width: int) -> None:
        """Add a collapsed disclosure showing the audio transcript sent with the message.

        Args:
            column (QVBoxLayout): The column to add the transcript and its toggle to.
            max_width (int): The widest the transcript may grow.
        """
        transcript_label = QLabel(self.transcript)
        transcript_label.setTextFormat(Qt.TextFormat.PlainText)
        transcript_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        transcript_label.setWordWrap(True)
        transcript_label.setMaximumWidth(max_width)
        transcript_label.setFont(QFont("Helvetica", 9))
        transcript_label.setStyleSheet(
            f"QLabel {{ color: {qss(PRIMARY_COLOR, 210)}; background: transparent; padding: 2px 5px; }}"
        )
        transcript_label.hide()
        toggle = QPushButton("Audio transcript ▾")
        toggle.setAccessibleName("Show audio transcript sent with this message")
        toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        toggle.setFont(QFont("Helvetica", 8))
        toggle.setStyleSheet(f"""
            QPushButton {{ color: {qss(PRIMARY_COLOR, 190)}; background: transparent; border: none; padding: 0 2px; }}
            QPushButton:hover {{ color: {qss(PRIMARY_COLOR)}; }}
        """)

        def on_toggle() -> None:
            """Show or hide the transcript."""
            transcript_label.setVisible(not transcript_label.isVisible())
            toggle.setText(
                f"Audio transcript {'▴' if transcript_label.isVisible() else '▾'}"
            )

        toggle.clicked.connect(on_toggle)
        column.addWidget(transcript_label, alignment=Qt.AlignmentFlag.AlignRight)
        column.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignRight)

    def _tick_loading(self) -> None:
        """Advance the loading animation by one frame."""
        t = self._loading_frame * (2 * math.pi / 20)  # 20 frames per cycle (~1.6 s)

        def dot_opacity(phase_offset: float) -> int:
            """Compute a smooth opacity value using a sine wave with a phase offset.

            Args:
                phase_offset (float): The phase offset in radians for this dot.

            Returns:
                int: Opacity in the range [26, 242].
            """
            return int(255 * (0.1 + 0.85 * (math.sin(t + phase_offset) + 1) / 2))

        o1 = dot_opacity(0)
        o2 = dot_opacity(2 * math.pi / 3)
        o3 = dot_opacity(4 * math.pi / 3)

        html = (
            f"<span style='color: rgba(255, 255, 255, {o1}); font-size: 16px;'>&#9679;</span>"
            f"&nbsp;"
            f"<span style='color: rgba(255, 255, 255, {o2}); font-size: 16px;'>&#9679;</span>"
            f"&nbsp;"
            f"<span style='color: rgba(255, 255, 255, {o3}); font-size: 16px;'>&#9679;</span>"
        )
        self.message_label.setText(html)
        self._loading_frame = (self._loading_frame + 1) % 20


class _ScreenshotPreview(QAbstractButton):
    """Show a small rounded preview of a sent screenshot that opens it when clicked."""

    def __init__(self, path: str, hidden: int, chat_bubble: QWidget) -> None:
        super().__init__(chat_bubble)
        self.hidden = hidden  # Screenshots after this one that have no preview
        self.hovered = False
        self.setFixedSize(PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pixmap = QPixmap(path)
        if not self.pixmap.isNull():
            self.pixmap = self.pixmap.scaled(
                PREVIEW_WIDTH,
                PREVIEW_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )

    def enterEvent(self, event: QEnterEvent | None) -> None:
        """Brighten the border while the pointer is over the preview.

        Args:
            event (QEnterEvent): The enter event.
        """
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent | None) -> None:
        """Restore the border once the pointer leaves the preview.

        Args:
            event (QEvent): The leave event.
        """
        self.hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Draw the screenshot clipped to rounded corners, with a count of any hidden ones."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 4.0, 4.0)
        painter.setClipPath(clip)
        if self.pixmap.isNull():
            painter.setPen(qcolor(PRIMARY_COLOR, 200))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Missing")
        else:
            # Center the expanded image so the crop is even on both sides
            painter.drawPixmap(
                (self.width() - self.pixmap.width()) // 2,
                (self.height() - self.pixmap.height()) // 2,
                self.pixmap,
            )
        if self.hidden:
            painter.fillRect(rect, qcolor(BG_COLOR, 170))
            painter.setPen(qcolor(PRIMARY_COLOR))
            painter.setFont(QFont("Helvetica", 10, QFont.Weight.Bold))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"+{self.hidden}")
        painter.setClipping(False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(qcolor(PRIMARY_COLOR, 200 if self.hovered else 90), 1.0))
        painter.drawRoundedRect(rect, 4.0, 4.0)
