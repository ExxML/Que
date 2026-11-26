import html
import math

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QEnterEvent,
    QFont,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QTextDocument,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.chat.ai_formatter import BODY_FONT, BODY_PT, format_message
from ui.theme import BG_COLOR, PRIMARY_COLOR, qcolor, qss

PREVIEW_WIDTH = 64
PREVIEW_HEIGHT = 36
PREVIEW_SPACING = 4
USER_WIDTH_RATIO = 0.75  # Widest a user message grows, as a share of the chat width


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
        # What the AI is doing, shown muted below the text until it writes again
        self.status = ""
        self.transcript_label: QLabel | None = None
        self._init_UI()

    @property
    def loading(self) -> bool:
        """Return whether the loading animation is running.

        Returns:
            bool: Whether the three-dot indicator is showing.
        """
        return self._loading_timer is not None

    def set_bot_message(self, message: str) -> None:
        """Format the bot message and set the text as the label.

        Args:
            message (str): The raw bot message text to format and display.
        """
        self.message = message
        self.message_label.setText(f"{format_message(message)}{self._status_html()}")

    def mark_stopped(self) -> None:
        """Show a muted note after the message that the reply was stopped before it finished."""
        note = f"<div style='color: {qss(PRIMARY_COLOR, 140)}; font-style: italic;'>Stopped.</div>"
        self.message_label.setText(
            f"{format_message(self.message)}{note}" if self.message else note
        )

    def line_at(self, global_position: QPoint) -> tuple[str, int] | None:
        """Find the shown line under a point, as an offset into the shown text.

        Args:
            global_position (QPoint): The point in global coordinates.

        Returns:
            tuple[str, int] | None: The shown text and where the line under the point starts,
                or None when the label has not laid its text out.
        """
        label = self.message_label
        documents = label.findChildren(QTextDocument)
        if not documents:
            return None
        document = documents[0]
        layout = document.documentLayout()
        assert layout is not None
        local = label.mapFromGlobal(global_position) - label.contentsRect().topLeft()
        # A fuzzy hit always finds the nearest character
        position = max(0, layout.hitTest(QPointF(local), Qt.HitTestAccuracy.FuzzyHit))
        block = document.findBlock(position)
        text_layout = block.layout()
        line = (
            text_layout.lineForTextPosition(position - block.position())
            if text_layout is not None
            else None
        )
        start = block.position() + (
            line.textStart() if line is not None and line.isValid() else 0
        )
        return document.toPlainText(), start

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

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Rewrap a user message to the new chat width.

        Args:
            event (QResizeEvent): The resize event.
        """
        super().resizeEvent(event)
        if self.is_user:
            self._fit_user_message(int(self.width() * USER_WIDTH_RATIO))

    def _init_UI(self) -> None:
        """Initialize the chat bubble UI layout and styling."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)
        # Let the chat narrow past the message's width so narrowing the window rewraps instead of clipping.
        # An explicit minimum, not an Ignored policy, which makes Qt lay every message out at zero width.
        self.setMinimumWidth(1)

        # Create message label with HTML formatting
        # Default formatting for user message only (bot message uses formatting in set_bot_message)
        html_message = f'<div style="white-space: pre-wrap;">{html.escape(self.message, quote=False)}</div>'
        self.message_label = _MessageLabel(html_message)
        self.message_label.setTextFormat(Qt.TextFormat.RichText)
        self.message_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        # Set font
        font = QFont(BODY_FONT)
        font.setPointSizeF(BODY_PT)
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
                    padding: 3px 8px 4px 8px;  /* top, right, bottom, left */
                }}
            """)  # padding adjusted to visually center text within the bubble

            # Let Qt estimate the unwrapped width of the message; resizeEvent wraps it to the chat width
            self.natural_width = self.message_label.sizeHint().width()
            max_width = 400  # Until the first resize gives the real chat width
            self._fit_user_message(max_width)

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
            # Fill the row whatever its width, as the bubble does
            self.message_label.setMinimumWidth(1)
            layout.addWidget(self.message_label)
            layout.setContentsMargins(
                5, 2, 19, 2
            )  # Keep the gap before the scrollbar that replies always had

        layout.setSpacing(0)

    def _fit_user_message(self, max_width: int) -> None:
        """Size a user message to its natural width, wrapping it when that is wider than allowed.

        Args:
            max_width (int): The widest the message and its transcript may grow.
        """
        wraps = self.natural_width > max_width
        self.message_label.setWordWrap(wraps)
        self.message_label.setFixedWidth(max_width if wraps else self.natural_width)
        if self.transcript_label is not None:
            self.transcript_label.setMaximumWidth(max_width)

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
        self.transcript_label = transcript_label = QLabel(self.transcript)
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
        self.message_label.setText(f"{html}{self._status_html()}")
        self._loading_frame = (self._loading_frame + 1) % 20

    def _status_html(self) -> str:
        """Render the current status as a muted line, or nothing when there is none.

        Returns:
            str: The status markup.
        """
        if not self.status:
            return ""
        return f"<div style='color: {qss(PRIMARY_COLOR, 140)}; font-style: italic;'>{html.escape(self.status, quote=False)}</div>"


class _MessageLabel(QLabel):
    """Show a message, laying its text out once per width.

    QLabel lays the whole rich text out again on every height query, and each resize step
    asks several times per message, so resizing a long chat lagged.

    A word wider than the label, such as a long code span or URL, breaks anywhere instead of
    overflowing. QLabel only breaks at spaces, which clipped such words and, inside a list,
    widened the whole list past the window so every line in it was cut off.
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.heights: dict[int, int] = {}
        # QLabel puts back its own wrap mode whenever it lays out new text, font or style
        self.wrap_pending = True

    def setText(self, text: str | None) -> None:
        """Show new text and forget the heights measured for the old one.

        Args:
            text (str | None): The rich text to show.
        """
        self.heights.clear()
        self.wrap_pending = True
        super().setText(text)

    def setWordWrap(self, on: bool) -> None:
        """Turn wrapping on or off and forget the heights measured with the old setting.

        Args:
            on (bool): Whether to wrap the text.
        """
        self.heights.clear()
        self.wrap_pending = True
        super().setWordWrap(on)

    def heightForWidth(self, width: int) -> int:
        """Return the text's height at a width, measuring it only the first time.

        Args:
            width (int): The width to lay the text out at.

        Returns:
            int: The height the text needs.
        """
        if width not in self.heights:
            self._break_long_words(width)
            self.heights[width] = super().heightForWidth(width)
        return self.heights[width]

    def changeEvent(self, event: QEvent | None) -> None:
        """Forget measured heights when the font or style changes how the text lays out.

        Args:
            event (QEvent): The change event.
        """
        if event is not None and event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.ContentsRectChange,
        ):
            self.heights.clear()
            self.wrap_pending = True
        super().changeEvent(event)

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Break long words again after a resize, which makes QLabel lay its text out afresh.

        Args:
            event (QResizeEvent): The resize event.
        """
        self.wrap_pending = True
        super().resizeEvent(event)

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Draw the text with long words broken, even if it changed since it was last measured.

        Args:
            event (QPaintEvent): The paint event.
        """
        self._break_long_words(self.width())
        super().paintEvent(event)

    def _break_long_words(self, width: int) -> None:
        """Let a word too wide for the label break anywhere, once QLabel has applied its own wrap mode.

        Args:
            width (int): The width QLabel lays the text out at while applying its wrap mode.
        """
        if not self.wrap_pending or not self.wordWrap():
            return
        self.wrap_pending = False
        # Let QLabel reset the wrap mode now rather than after this, then override it in its one document
        super().heightForWidth(width)
        for document in self.findChildren(QTextDocument):
            option = document.defaultTextOption()
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            document.setDefaultTextOption(option)


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
