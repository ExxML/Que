from math import exp

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPixmap,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import QGraphicsEffect, QScrollArea, QVBoxLayout, QWidget

from ui.chat.chat_bubble import ChatBubble

RENDER_INTERVAL = 50  # Milliseconds between redraws of a streaming reply; laying out long replies is slow

SCROLLBAR_STYLE = """
    QScrollBar:vertical {
        background-color: rgba(0, 0, 0, 0);
        width: 5px;
    }
    QScrollBar::handle:vertical {
        background-color: rgba(255, 255, 255, 77);
        border-radius: 2px;
        min-height: 20px;
    }
    QScrollBar::handle:vertical:hover {
        background-color: rgba(255, 255, 255, 128);
    }
    QScrollBar::handle:vertical:disabled {
        background-color: transparent;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: none;
    }
"""


class ChatArea(QScrollArea):
    """Display message history in a scrollable chat area."""

    screenshot_opened = pyqtSignal(list, int)  # Screenshot paths, index to show
    reply_changed = pyqtSignal(
        object, str, bool
    )  # Reply bubble, its Markdown, whether finished
    messages_cleared = pyqtSignal()

    def __init__(self, main_window: QWidget) -> None:
        super().__init__(main_window)
        self.streaming_bubble: ChatBubble | None = None
        self.streaming_text: str = ""
        self.pending_message_scroll: ChatBubble | None = None
        self.reply_scroll_anchor: ChatBubble | None = None
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(RENDER_INTERVAL)
        self.render_timer.timeout.connect(self._render_stream)
        self._init_UI()
        self._init_scroll_animation()
        self._reset_stream()

    def add_message(
        self,
        message: str,
        is_user: bool,
        transcript: str = "",
        screenshots: tuple[str, ...] = (),
    ) -> None:
        """Add a new message to the chat area.

        Args:
            message (str): The message text to display.
            is_user (bool): Whether the message is from the user.
            transcript (str): Audio transcript sent with a user message.
            screenshots (tuple[str, ...]): Screenshot files sent with a user message.
        """
        self.pending_message_scroll = None
        # Remove the stretch before adding new message
        self.chat_layout.takeAt(self.chat_layout.count() - 1)

        # Create and add the chat bubble
        bubble = self._bubble(message, is_user, transcript, screenshots)
        self.chat_layout.addWidget(bubble)

        # Pre-create the assistant bubble for loading and tool status
        if is_user:
            self.streaming_bubble = ChatBubble("", is_user=False)
            self.streaming_text = ""
            self.chat_layout.addWidget(self.streaming_bubble)
            self.streaming_bubble.start_loading_animation()
            self._queue_message_scroll(bubble)

        # Add stretch back at the end
        self.chat_layout.addStretch()

    def start_assistant_stream(self) -> None:
        """Create an assistant bubble to stream content into (not saved until finalized)."""
        if self.streaming_bubble is not None:
            return
        # Remove stretch, add empty assistant bubble, then add stretch back
        self.chat_layout.takeAt(self.chat_layout.count() - 1)
        self.streaming_bubble = ChatBubble("", is_user=False)
        self.streaming_text = ""
        self.chat_layout.addWidget(self.streaming_bubble)
        self.chat_layout.addStretch()

    def append_to_stream(self, chunk_text: str) -> None:
        """Append text to the current streaming assistant bubble.

        Args:
            chunk_text (str): The text chunk to append to the stream.
        """
        if self.streaming_bubble is None:
            return
        first = not self.streaming_text or self.streaming_text.isspace()
        self.streaming_text += chunk_text
        # Before the redraw throttle, so speech reaches each new word at once
        self.reply_changed.emit(self.streaming_bubble, self.streaming_text, False)
        if first and not chunk_text.strip():
            return

        self.streaming_bubble.status = ""  # New text ends the current tool status
        # Show the first words at once, then redraw at most once per interval
        if first:
            self.streaming_bubble.stop_loading_animation()
            self._render_stream()
            self._queue_message_scroll(self.streaming_bubble)
        elif not self.render_timer.isActive():
            self.render_timer.start()

    def set_stream_status(self, status: str) -> None:
        """Show what the AI is doing below the streamed text until its next text arrives.

        Args:
            status (str): The status, such as the file being read.
        """
        if self.streaming_bubble is None:
            return
        self.streaming_bubble.status = status
        # The loading animation draws the status with its next frame
        if not self.streaming_bubble.loading:
            self.render_timer.stop()
            self.streaming_bubble.set_bot_message(self.streaming_text)

    def finalize_assistant_stream(self) -> None:
        """Finalize the streamed assistant message and clear streaming state."""
        if self.streaming_bubble is None:
            return
        self.streaming_bubble.stop_loading_animation()
        status, self.streaming_bubble.status = self.streaming_bubble.status, ""
        # Draw text still waiting for the redraw interval, and drop any finished tool status
        if self.streaming_text or status:
            self.streaming_bubble.set_bot_message(self.streaming_text)
        self.reply_changed.emit(self.streaming_bubble, self.streaming_text, True)
        self._reset_stream()

    def stop_assistant_stream(self) -> None:
        """Mark the streamed assistant message as stopped and clear streaming state."""
        if self.streaming_bubble is None:
            return
        self.streaming_bubble.stop_loading_animation()
        self.streaming_bubble.status = ""
        self.streaming_bubble.message = self.streaming_text
        self.streaming_bubble.mark_stopped()
        self.reply_changed.emit(self.streaming_bubble, self.streaming_text, True)
        self._reset_stream()

    def show_stream_error(self, error_msg: str) -> None:
        """Display an error message in the current streaming bubble, replacing the loading indicator.

        If no streaming bubble exists, falls back to adding a new bot message.

        Args:
            error_msg (str): The error text to display.
        """
        if self.streaming_bubble is None:
            self.add_message(error_msg, is_user=False)
            return
        self.streaming_bubble.stop_loading_animation()
        self.streaming_bubble.status = ""
        # Speech ends with what streamed before the error, never reading the error itself
        self.reply_changed.emit(self.streaming_bubble, self.streaming_text, True)
        self.streaming_bubble.set_bot_message(error_msg)
        self._reset_stream()

    def clear_chat_messages(self) -> None:
        """Clear all messages from the chat area."""
        # Reset streaming state before deleting widgets to avoid dangling references
        self._reset_stream()
        self.messages_cleared.emit()
        self.pending_message_scroll = None
        self.reply_scroll_anchor = None
        self.chat_container.setMinimumHeight(0)

        # Remove all widgets except the stretch
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

    def load_messages(
        self, messages: list[tuple[str, bool, str, tuple[str, ...]]]
    ) -> None:
        """Replace the chat with a saved transcript and scroll to its end.

        Args:
            messages (list[tuple[str, bool, str, tuple[str, ...]]]): Message texts with whether
                each is from the user, its audio transcript, and its screenshot files.
        """
        self.clear_chat_messages()
        for message, is_user, transcript, screenshots in messages:
            bubble = self._bubble(message, is_user, transcript, screenshots)
            if not is_user:
                bubble.set_bot_message(message)
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        # Bound methods, unlike lambdas, are dropped if this widget is deleted before the timer fires
        QTimer.singleShot(0, self._jump_to_bottom)

    def latest_reply(self) -> ChatBubble | None:
        """Find the newest AI reply, including one still waiting for its first words.

        Returns:
            ChatBubble | None: The reply, or None in an empty chat.
        """
        if self.streaming_bubble is not None:
            return self.streaming_bubble
        for index in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, ChatBubble) and not widget.is_user:
                return widget
        return None

    def reply_text(self, chat_bubble: ChatBubble) -> tuple[str, bool]:
        """Return a reply's Markdown so far and whether it has finished streaming.

        Args:
            chat_bubble (ChatBubble): The reply.

        Returns:
            tuple[str, bool]: The Markdown, and whether the reply has finished.
        """
        if chat_bubble is self.streaming_bubble:
            return self.streaming_text, False
        return chat_bubble.message, True

    def shortcut_scroll(self, amount: int) -> None:
        """Scroll the chat area by a specified amount.

        Args:
            amount (int): The pixel amount to scroll (positive for down, negative for up).
        """
        target = self.scrollbar.value() + amount
        duration = 100
        self._animate_to(target, duration)

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Refit the reply's trailing space after the viewport relayout."""
        super().resizeEvent(event)
        if self.reply_scroll_anchor is not None:
            QTimer.singleShot(0, self._refit_reply_scroll_space)

    def showEvent(self, event: QShowEvent | None) -> None:
        """Schedule any message scroll that was waiting for the chat to become visible."""
        super().showEvent(event)
        if self.pending_message_scroll is not None:
            QTimer.singleShot(0, self._scroll_to_pending_message)

    def _init_UI(self) -> None:
        """Initialize the chat area UI layout, scroll settings, and styling."""
        # Configure scroll area
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        # Create container widget for messages
        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background-color: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(3, 3, 3, 3)
        self.chat_layout.addStretch()

        # Set the container as the scroll area's widget
        self.setWidget(self.chat_container)

        scrollbar = self.verticalScrollBar()
        assert scrollbar is not None
        self.scrollbar = scrollbar

        # Style the scroll area
        self.setStyleSheet(
            """
            QScrollArea {
                background-color: transparent;
                border: none;
            }
        """
            + SCROLLBAR_STYLE
        )

        # Apply fade effect to the top and bottom edges of the viewport
        viewport = self.viewport()
        assert viewport is not None
        self.fade_effect = _FadeOverlay()
        viewport.setGraphicsEffect(self.fade_effect)

        # Connect scroll signals for position-aware fading.
        # Defer via singleShot(0) so the scrollbar value is read after Qt has
        # finished applying all pending layout/range adjustments.
        self.scrollbar.valueChanged.connect(
            lambda _val: QTimer.singleShot(0, self._update_fade_visibility)
        )
        self.scrollbar.rangeChanged.connect(
            lambda _min, _max: QTimer.singleShot(0, self._update_fade_visibility)
        )

    def _bubble(
        self,
        message: str,
        is_user: bool,
        transcript: str,
        screenshots: tuple[str, ...],
    ) -> ChatBubble:
        """Create a message bubble whose screenshot previews open through this chat area.

        Args:
            message (str): The message text to display.
            is_user (bool): Whether the message is from the user.
            transcript (str): Audio transcript sent with a user message.
            screenshots (tuple[str, ...]): Screenshot files sent with a user message.

        Returns:
            ChatBubble: The new bubble.
        """
        bubble = ChatBubble(message, is_user, transcript, screenshots)
        bubble.screenshot_opened.connect(self.screenshot_opened)
        return bubble

    def _update_fade_visibility(self) -> None:
        """Scale each fade edge based on distance from the nearest scroll edge."""
        sb = self.scrollbar
        fade_start = self.fade_effect.FADE_START_DIST
        top_dist = sb.value() - sb.minimum()
        bottom_dist = sb.maximum() - sb.value()
        self.fade_effect.top_strength = min(1.0, max(0.0, top_dist / fade_start))
        self.fade_effect.bottom_strength = min(1.0, max(0.0, bottom_dist / fade_start))
        self.fade_effect.update()

    def _queue_message_scroll(self, chat_bubble: ChatBubble) -> None:
        """Scroll to the latest target after Qt has laid out the new content.

        Args:
            chat_bubble (ChatBubble): The sent message or the start of its reply.
        """
        self.pending_message_scroll = chat_bubble
        QTimer.singleShot(0, self._scroll_to_pending_message)

    def _scroll_to_pending_message(self) -> None:
        """Reveal the latest sent message or align the start of the reply."""
        bubble = self.pending_message_scroll
        if bubble is None or not self.isVisible():
            return
        self.pending_message_scroll = None
        self.scroll_animation.stop()
        if not bubble.is_user:
            self._scroll_to_reply_start(bubble)
            return

        # The new bubble can still be compressed into the old scroll range.
        # Lay out the full content before reading the bubble's bottom edge.
        width = self.chat_container.width()
        height = self.chat_layout.totalHeightForWidth(width)
        self.chat_container.resize(width, max(height, self.chat_container.height()))
        self.chat_layout.activate()
        viewport = self.viewport()
        assert viewport is not None
        message_bottom = bubble.y() + bubble.height()
        self.scrollbar.setValue(
            message_bottom + self.fade_effect.FADE_HEIGHT - viewport.height()
        )

    def _scroll_to_reply_start(self, chat_bubble: ChatBubble) -> None:
        """Place the first line of the latest reply just below the top fade.

        Args:
            chat_bubble (ChatBubble): The assistant reply to bring into view.
        """
        self.reply_scroll_anchor = chat_bubble
        # Measure at the natural layout height. The reply's current position can be
        # stale or compressed until the container has space for the new content.
        width = self.chat_container.width()
        margins = self.chat_layout.contentsMargins()
        reply_height = chat_bubble.heightForWidth(
            width - margins.left() - margins.right()
        )
        reply_top = (
            self.chat_layout.totalHeightForWidth(width)
            - margins.bottom()
            - reply_height
        )
        self.scrollbar.setValue(self._reserve_reply_scroll_space(reply_top))

    def _refit_reply_scroll_space(self) -> None:
        """Adjust the reserved space after rewrapping the conversation."""
        if self.reply_scroll_anchor is not None:
            self.chat_layout.activate()
            self._reserve_reply_scroll_space(self.reply_scroll_anchor.y())

    def _reserve_reply_scroll_space(self, reply_top: int) -> int:
        """Give the trailing stretch enough space for a short reply to reach the top.

        Args:
            reply_top (int): The reply's top edge in the chat layout.

        Returns:
            int: The scroll position that places the reply below the top fade.
        """
        viewport = self.viewport()
        assert viewport is not None
        target = max(0, reply_top - self.fade_effect.FADE_HEIGHT)
        self.chat_container.setMinimumHeight(target + viewport.height())
        return target

    def _jump_to_bottom(self) -> None:
        """Scroll to the newest message at once."""
        self.scrollbar.setValue(self.scrollbar.maximum())

    def _reset_stream(self) -> None:
        """Reset the streaming bubble and accumulated text to a blank state."""
        self.render_timer.stop()
        self.streaming_bubble = None
        self.streaming_text = ""

    def _render_stream(self) -> None:
        """Draw the streamed text received so far."""
        self.render_timer.stop()
        if self.streaming_bubble is not None and self.streaming_text:
            self.streaming_bubble.set_bot_message(self.streaming_text)

    def _init_scroll_animation(self) -> None:
        """Initialize smooth scrolling animation."""
        self.scroll_animation = QPropertyAnimation(self.scrollbar, b"value", self)
        self.scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.scroll_target: int = 0

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        """Handle mouse wheel events with smooth animated scrolling.

        Accumulates the running animation target so rapid wheel spins
        chain naturally instead of restarting from the current position.

        Args:
            event (QWheelEvent): The wheel event that drives the scroll delta.
        """
        if event is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        # Scale factor: 120 units per standard notch → ~80 px per notch.
        step = round(-delta * 2 / 3)
        if step == 0:
            event.accept()
            return
        anim = self.scroll_animation
        base = (
            self.scroll_target
            if anim.state() == QAbstractAnimation.State.Running
            else self.scrollbar.value()
        )
        sb = self.scrollbar
        self.scroll_target = max(sb.minimum(), min(base + step, sb.maximum()))
        self._animate_to(self.scroll_target, 150)
        event.accept()

    def _animate_to(self, target: int, duration: int) -> None:
        """Animate scrollbar to target position.

        Args:
            target (int): The target scroll position.
            duration (int): The animation duration in milliseconds.
        """
        anim = self.scroll_animation
        if anim.state() == QAbstractAnimation.State.Running:
            anim.stop()
        sb = self.scrollbar
        target = max(sb.minimum(), min(target, sb.maximum()))
        anim.setTargetObject(sb)
        anim.setStartValue(sb.value())
        anim.setEndValue(target)
        anim.setDuration(duration)
        anim.start()


class _FadeOverlay(QGraphicsEffect):
    """Fade the top and bottom edges of a widget to transparent."""

    FADE_HEIGHT = 30
    FADE_START_DIST = FADE_HEIGHT // 2
    FADE_STEPS = 10
    FADE_EXPONENT = 2.5

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.top_strength = 0.0
        self.bottom_strength = 0.0

    def _set_exponential_stops(
        self, gradient: QLinearGradient, *, edge_at_start: bool, strength: float
    ) -> None:
        """Populate gradient stops every 0.1 with exponential edge fade."""
        scale = exp(self.FADE_EXPONENT) - 1.0

        for step in range(self.FADE_STEPS + 1):
            position = step / self.FADE_STEPS
            distance = position if edge_at_start else 1.0 - position
            full_fade_alpha = round(
                255 * ((exp(self.FADE_EXPONENT * distance) - 1.0) / scale)
            )
            alpha = round(255 - strength * (255 - full_fade_alpha))
            gradient.setColorAt(position, QColor(0, 0, 0, alpha))

    def draw(self, painter: QPainter | None) -> None:
        """Draw the source widget with faded top and bottom edges.

        Args:
            painter (QPainter): The painter used to render the effect output.
        """
        if painter is None:
            return

        result: tuple[QPixmap, QPoint | None] = self.sourcePixmap(
            Qt.CoordinateSystem.LogicalCoordinates
        )
        pixmap, offset = result

        if pixmap.isNull() or offset is None:
            return

        # QPainter on a high-DPR pixmap operates in logical coordinates, so
        # we must derive logical dimensions from the raw pixel size.
        dpr = pixmap.devicePixelRatio()
        height = pixmap.height() / dpr
        width = pixmap.width() / dpr
        fade_px = float(self.FADE_HEIGHT)

        if height > 2 * fade_px:
            fade_painter = QPainter(pixmap)
            fade_painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn
            )

            if self.top_strength > 0.0:
                top_gradient = QLinearGradient(0, 0, 0, fade_px)
                self._set_exponential_stops(
                    top_gradient,
                    edge_at_start=True,
                    strength=self.top_strength,
                )
                fade_painter.fillRect(QRectF(0, 0, width, fade_px), top_gradient)

            if self.bottom_strength > 0.0:
                bottom_gradient = QLinearGradient(0, height - fade_px, 0, height)
                self._set_exponential_stops(
                    bottom_gradient,
                    edge_at_start=False,
                    strength=self.bottom_strength,
                )
                fade_painter.fillRect(
                    QRectF(0, height - fade_px, width, fade_px),
                    bottom_gradient,
                )

            fade_painter.end()

        painter.drawPixmap(offset, pixmap)
