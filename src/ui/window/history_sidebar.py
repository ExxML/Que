import time
from typing import Callable

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QPointF,
    Qt,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.chat_history import Conversation
from ui.theme import (
    DANGER_COLOR,
    PRIMARY_COLOR,
    PROVIDER_COLORS,
    qcolor,
    qss,
)


def format_age(timestamp: float, now: float) -> str:
    """Describe how long ago a timestamp occurred in compact form.

    Args:
        timestamp (float): The Unix time of the event.
        now (float): The current Unix time.

    Returns:
        str: A short relative age such as "5m ago", or a date for older events.
    """
    seconds = max(0, int(now - timestamp))
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d ago"
    local = time.localtime(timestamp)
    return f"{time.strftime('%b', local)} {local.tm_mday}"


class HistorySidebar(QWidget):
    """Slide a conversation history drawer over the left side of the window.

    The drawer replaces the pixels beneath it with the window background instead of
    blending over them, so the translucent overlay never compounds into a darker layer.
    The chat beside it stays interactive; Escape closes the drawer.
    """

    conversation_selected = pyqtSignal(str)
    conversation_deleted = pyqtSignal(str)
    closed = pyqtSignal()

    PANEL_WIDTH = 220

    def __init__(
        self, main_window: QWidget, paint_frame: Callable[[QPainter], None]
    ) -> None:
        super().__init__(main_window)
        self.paint_frame = paint_frame  # Paints the window background and border
        self.opened = False
        self.progress = 0.0
        self.panel = QWidget(self)
        self.conversation_list = QListWidget(self.panel)
        self.empty_label = QLabel(
            "No conversations yet.\nChats are saved here automatically.", self.panel
        )
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._on_animation_step)
        self.animation.finished.connect(self._on_animation_finished)
        self._init_UI()
        self.hide()

    def toggle(self) -> None:
        """Open the drawer when closed, otherwise close it."""
        if self.opened:
            self.close_sidebar()
        else:
            self.open_sidebar()

    def open_sidebar(self) -> None:
        """Slide the drawer in and focus the conversation list."""
        self.opened = True
        self.show()
        self.raise_()
        self._animate(1.0)
        self.conversation_list.setFocus()

    def close_sidebar(self) -> None:
        """Slide the drawer out and hand focus back to the window."""
        if not self.opened:
            return
        self.opened = False
        self._animate(0.0)
        self.closed.emit()

    def set_conversations(
        self, conversations: list[Conversation], active_id: str
    ) -> None:
        """Rebuild the list of saved conversations.

        Args:
            conversations (list[Conversation]): Saved conversations, newest first.
            active_id (str): The identifier of the open conversation, or an empty string.
        """
        self.conversation_list.clear()
        now = time.time()
        for conversation in conversations:
            item = QListWidgetItem(self.conversation_list)
            item.setData(Qt.ItemDataRole.UserRole, conversation.conversation_id)
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{conversation.title}, {conversation.provider.title()}, "
                f"{format_age(conversation.updated, now)}",
            )
            item.setToolTip(conversation.title)
            row = _ConversationRow(conversation, now, self.conversation_list)
            row.delete_requested.connect(self.conversation_deleted)
            item.setSizeHint(row.sizeHint())
            self.conversation_list.setItemWidget(item, row)
            if conversation.conversation_id == active_id:
                self.conversation_list.setCurrentItem(item)
        self.empty_label.setVisible(not conversations)
        self.conversation_list.setVisible(bool(conversations))

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        """Close on Escape and delete the selected conversation on Delete.

        Args:
            event (QKeyEvent): The key press event.
        """
        if event is None:
            return
        if event.key() == Qt.Key.Key_Escape:
            self.close_sidebar()
        elif event.key() == Qt.Key.Key_Delete:
            row = self._current_row()
            if row is not None:
                row.request_delete()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Paint the drawer as the bare window background, border included."""
        painter = QPainter(self)
        panel_rect = self.panel.geometry()

        # Clear the chat under the drawer, then repaint the window's own frame there, so
        # translucent layers never stack and the window border stays pixel-identical
        painter.setClipRect(panel_rect)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(panel_rect, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(-QPointF(self.pos()))
        self.paint_frame(painter)
        painter.resetTransform()
        painter.setPen(qcolor(PRIMARY_COLOR, 40))
        painter.drawLine(panel_rect.topRight(), panel_rect.bottomRight())

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Keep the drawer at full height when the window is resized.

        Args:
            event (QResizeEvent): The resize event.
        """
        super().resizeEvent(event)
        self.panel.resize(self.PANEL_WIDTH, self.height())

    def _init_UI(self) -> None:
        """Build the drawer heading, list, and empty state."""
        self.panel.setGeometry(-self.PANEL_WIDTH, 0, self.PANEL_WIDTH, self.height())
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        heading = QLabel("History", self.panel)
        heading.setStyleSheet(
            f"color: {qss(PRIMARY_COLOR, 140)}; font-size: 11px; font-weight: bold; padding-left: 4px;"
        )
        layout.addWidget(heading)

        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(
            f"color: {qss(PRIMARY_COLOR, 120)}; padding: 24px 8px;"
        )
        layout.addWidget(self.empty_label)

        self.conversation_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.conversation_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel
        )
        self.conversation_list.setAccessibleName("Saved conversations")
        self.conversation_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{ border-radius: 6px; margin: 1px 0px; }}
            QListWidget::item:hover {{ background: {qss(PRIMARY_COLOR, 18)}; }}
            QListWidget::item:selected {{ background: {qss(PRIMARY_COLOR, 36)}; }}
            QScrollBar:vertical {{ background: transparent; width: 5px; }}
            QScrollBar::handle:vertical {{
                background: {qss(PRIMARY_COLOR, 77)}; border-radius: 2px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
        """)
        self.conversation_list.itemClicked.connect(self._on_item_chosen)
        self.conversation_list.itemActivated.connect(self._on_item_chosen)
        self.conversation_list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self.conversation_list, stretch=1)
        layout.addStretch()

    def _animate(self, target: float) -> None:
        """Animate the drawer from its current position to the target.

        Args:
            target (float): 1.0 for fully open or 0.0 for fully closed.
        """
        self.animation.stop()
        self.animation.setStartValue(self.progress)
        self.animation.setEndValue(target)
        self.animation.start()

    def _on_animation_step(self, value: float) -> None:
        """Move the drawer and fade the backdrop for one animation frame.

        Args:
            value (float): The open fraction between 0.0 and 1.0.
        """
        self.progress = float(value)
        self.panel.move(round((self.progress - 1.0) * self.PANEL_WIDTH), 0)
        self.update()

    def _on_animation_finished(self) -> None:
        """Hide the overlay once the closing animation completes."""
        if not self.opened:
            self.hide()

    def _on_item_chosen(self, item: QListWidgetItem | None) -> None:
        """Request opening the clicked or activated conversation.

        Args:
            item (QListWidgetItem): The chosen list item.
        """
        if item is not None:
            self.conversation_selected.emit(item.data(Qt.ItemDataRole.UserRole))

    def _on_current_changed(
        self, _current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        """Cancel a pending delete confirmation when keyboard focus moves away.

        Args:
            previous (QListWidgetItem): The previously selected item.
        """
        if previous is not None:
            row = self.conversation_list.itemWidget(previous)
            if isinstance(row, _ConversationRow):
                row.disarm()

    def _current_row(self) -> "_ConversationRow | None":
        """Return the row widget of the selected conversation.

        Returns:
            _ConversationRow | None: The selected row, or None when nothing is selected.
        """
        item = self.conversation_list.currentItem()
        row = self.conversation_list.itemWidget(item) if item is not None else None
        return row if isinstance(row, _ConversationRow) else None


class _ConversationRow(QWidget):
    """Show a conversation's title, provider, and age with a two-step delete button."""

    delete_requested = pyqtSignal(str)

    TITLE_WIDTH = 150

    def __init__(self, conversation: Conversation, now: float, parent: QWidget) -> None:
        super().__init__(parent)
        self.conversation_id = conversation.conversation_id
        self.armed = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        layout.setSpacing(4)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        title = QLabel(self)
        title.setText(
            title.fontMetrics().elidedText(
                conversation.title, Qt.TextElideMode.ElideRight, self.TITLE_WIDTH
            )
        )
        title.setStyleSheet(
            f"color: {qss(PRIMARY_COLOR, 230)}; background: transparent;"
        )
        # Let the wider confirmation button take space from the title instead of overflowing
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        provider_color = qss(PROVIDER_COLORS.get(conversation.provider, PRIMARY_COLOR))
        meta = QLabel(
            f'<span style="color: {provider_color};">&#9679; {conversation.provider.title()}</span>'
            f" &middot; {format_age(conversation.updated, now)}",
            self,
        )
        meta.setStyleSheet(
            f"color: {qss(PRIMARY_COLOR, 120)}; font-size: 11px; background: transparent;"
        )
        for label in (title, meta):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            text_layout.addWidget(label)
        layout.addLayout(text_layout, stretch=1)

        self.delete_button = QPushButton(self)
        self.delete_button.setAccessibleName("Delete conversation")
        self.delete_button.setToolTip("Delete conversation")
        self.delete_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.delete_button.clicked.connect(self.request_delete)
        layout.addWidget(self.delete_button, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.disarm()

    def request_delete(self) -> None:
        """Ask for confirmation on the first request and delete on the second."""
        if self.armed:
            self.delete_requested.emit(self.conversation_id)
            return
        self.armed = True
        self.delete_button.setText("Delete")
        self.delete_button.setToolTip("Click again to delete permanently")
        self.delete_button.setStyleSheet(f"""
            QPushButton {{
                color: {qss(DANGER_COLOR)}; background: {qss(DANGER_COLOR, 40)};
                border: none; border-radius: 4px; padding: 2px 6px; font-size: 11px;
            }}
        """)

    def disarm(self) -> None:
        """Return the delete button to its unconfirmed state."""
        self.armed = False
        self.delete_button.setText("×")
        self.delete_button.setToolTip("Delete conversation")
        self.delete_button.setStyleSheet(f"""
            QPushButton {{
                color: {qss(PRIMARY_COLOR, 90)}; background: transparent;
                border: none; border-radius: 4px; padding: 0px 6px; font-size: 16px;
            }}
            QPushButton:hover {{ color: {qss(DANGER_COLOR)}; background: {qss(DANGER_COLOR, 40)}; }}
        """)

    def leaveEvent(self, event: QEvent | None) -> None:
        """Cancel a pending delete confirmation when the pointer leaves the row.

        Args:
            event (QEvent): The leave event.
        """
        self.disarm()
        super().leaveEvent(event)
