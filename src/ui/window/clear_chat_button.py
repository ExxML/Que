import os
from typing import Callable

from PyQt6.QtCore import QEvent, QSize
from PyQt6.QtGui import QColor, QEnterEvent, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QPushButton, QWidget

from ui.chat.chat_area import ChatArea
from ui.theme import PRIMARY_COLOR, qcolor


class ClearChatButton(QPushButton):
    """Clear the chat history from the title bar."""

    def __init__(
        self, main_window: QWidget, on_click: Callable[[], None], chat_area: ChatArea
    ) -> None:
        super().__init__("", main_window)
        self.chat_area = chat_area
        self.setFixedSize(36, 32)
        self.clicked.connect(on_click)

        icon_path = os.path.join(
            os.getcwd(), "src", "assets", "clear_chat_button_icon.png"
        )
        self.light_icon = self._tint_icon(icon_path, qcolor(PRIMARY_COLOR, 77))
        self.dark_icon = self._tint_icon(icon_path, QColor(0, 0, 0, 153))
        self.setIcon(self.light_icon)
        self.setIconSize(QSize(14, 14))
        self.setStyleSheet("""
            QPushButton {
                border: none;
            }
            QPushButton:hover {
                background-color: rgba(255, 77, 69, 128);
                border-radius: 0px;
                border-top-left-radius: 7px;
                margin-top: 1px;
                margin-left: 1px;
            }
        """)

    # Override to change icon on hover
    def enterEvent(self, event: QEnterEvent | None) -> None:
        """Change the button icon to the dark variant on hover.

        Args:
            event (QEnterEvent): The mouse enter event.
        """
        self.setIcon(self.dark_icon)
        super().enterEvent(event)

    # Override to change icon when not on hover
    def leaveEvent(self, event: QEvent | None) -> None:
        """Restore the button icon to the light variant when hover ends.

        Args:
            event (QEvent): The mouse leave event.
        """
        self.setIcon(self.light_icon)
        super().leaveEvent(event)

    def _tint_icon(self, icon_path: str, color: QColor) -> QIcon:
        """Recolor the icon at the given path to a solid shade.

        Args:
            icon_path (str): The path to the source icon image.
            color (QColor): The shade to apply to the icon's opaque pixels.

        Returns:
            QIcon: The recolored icon.
        """
        pixmap = QPixmap(icon_path)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()
        return QIcon(pixmap)
