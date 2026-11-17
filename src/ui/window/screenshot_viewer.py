from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from ui.theme import PRIMARY_COLOR, qcolor, qss

BUTTON_SIZE = 28
MARGIN = 8


class ScreenshotViewer(QWidget):
    """Show sent screenshots enlarged over the chat, inside Que's capture-excluded window.

    Like the history drawer, the viewer replaces the pixels beneath it with the window
    background instead of blending over them, so translucent layers never stack. Escape
    or a click anywhere but its buttons closes it; the arrow keys step through screenshots.
    """

    def __init__(
        self, main_window: QWidget, paint_frame: Callable[[QPainter], None]
    ) -> None:
        super().__init__(main_window)
        self.paint_frame = paint_frame  # Paints the window background and border
        self.paths: list[str] = []
        self.index = 0
        self.pixmap = QPixmap()
        self.scaled = QPixmap()  # The pixmap fitted to the current image area
        self.close_button = self._button("×", "Close screenshot", "Close (Esc)")
        self.close_button.clicked.connect(self.close_viewer)
        self.previous_button = self._button("‹", "Previous screenshot", "Previous (←)")
        self.previous_button.clicked.connect(lambda: self._step(-1))
        self.next_button = self._button("›", "Next screenshot", "Next (→)")
        self.next_button.clicked.connect(lambda: self._step(1))
        self.counter = QLabel(self)
        self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter.setStyleSheet(
            f"QLabel {{ color: {qss(PRIMARY_COLOR, 200)}; background: transparent; }}"
        )
        self.hide()

    def open_screenshots(self, paths: list[str], index: int) -> None:
        """Show one of a message's screenshots, with controls to step through the rest.

        Args:
            paths (list[str]): The message's screenshot files.
            index (int): The screenshot to show first.
        """
        self.paths = paths
        many = len(paths) > 1
        self.previous_button.setVisible(many)
        self.next_button.setVisible(many)
        self.counter.setVisible(many)
        self._show_index(index)
        if not self.isVisible():
            app = QApplication.instance()
            assert app is not None
            app.installEventFilter(self)
        self.show()
        self.raise_()

    def close_viewer(self) -> None:
        """Hide the viewer and release the full-size image."""
        if not self.isVisible():
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.hide()
        self.pixmap = QPixmap()
        self.scaled = QPixmap()

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """Close on Escape and step on the arrow keys, wherever Que's keyboard focus is.

        Args:
            watched (QObject): The object receiving the event.
            event (QEvent): The event being processed.

        Returns:
            bool: Whether the key was used by the viewer.
        """
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close_viewer()
                return True
            if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                self._step(-1 if event.key() == Qt.Key.Key_Left else 1)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        """Close the viewer on a click outside its buttons.

        Args:
            event (QMouseEvent): The mouse press event.
        """
        self.close_viewer()
        if event is not None:
            event.accept()

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Keep the controls at the edges and refit the image.

        Args:
            event (QResizeEvent): The resize event.
        """
        super().resizeEvent(event)
        self.close_button.move(self.width() - BUTTON_SIZE - MARGIN, MARGIN)
        middle = (self.height() - BUTTON_SIZE) // 2
        self.previous_button.move(MARGIN, middle)
        self.next_button.move(self.width() - BUTTON_SIZE - MARGIN, middle)
        self.counter.setGeometry(0, self.height() - MARGIN - 20, self.width(), 20)
        self.scaled = QPixmap()

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Paint the bare window background with the screenshot fitted in its center."""
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.translate(-QPointF(self.pos()))
        self.paint_frame(painter)
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self._image_area()
        if self.pixmap.isNull():
            painter.setPen(qcolor(PRIMARY_COLOR, 200))
            painter.drawText(
                area,
                Qt.AlignmentFlag.AlignCenter,
                "This screenshot is no longer saved.",
            )
            return
        if self.scaled.isNull():
            self.scaled = self.pixmap.scaled(
                area.size() * self.devicePixelRatio(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.scaled.setDevicePixelRatio(self.devicePixelRatio())
        size = self.scaled.deviceIndependentSize()
        image = QRectF(
            area.x() + (area.width() - size.width()) / 2,
            area.y() + (area.height() - size.height()) / 2,
            size.width(),
            size.height(),
        )
        clip = QPainterPath()
        clip.addRoundedRect(image, 6.0, 6.0)
        painter.setClipPath(clip)
        painter.drawPixmap(image.topLeft(), self.scaled)
        painter.setClipping(False)
        painter.setPen(QPen(qcolor(PRIMARY_COLOR, 90), 1.0))
        painter.drawRoundedRect(image.adjusted(0.5, 0.5, -0.5, -0.5), 6.0, 6.0)

    def _button(self, text: str, name: str, tip: str) -> QPushButton:
        """Create a round, frameless viewer control.

        Args:
            text (str): The glyph shown on the button.
            name (str): The accessible name.
            tip (str): The tooltip, naming its keyboard shortcut.

        Returns:
            QPushButton: The control.
        """
        button = QPushButton(text, self)
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAccessibleName(name)
        button.setToolTip(tip)
        # A border, not a fill, marks hover so no translucent layer stacks on the window
        button.setStyleSheet(f"""
            QPushButton {{
                color: {qss(PRIMARY_COLOR, 220)};
                background: transparent;
                border: 1px solid transparent;
                border-radius: {BUTTON_SIZE // 2}px;
                font-size: 20px;
                padding-bottom: 3px;
            }}
            QPushButton:hover {{ border-color: {qss(PRIMARY_COLOR, 120)}; }}
        """)
        return button

    def _image_area(self) -> QRect:
        """Return the area the screenshot is fitted into, clear of the controls.

        Returns:
            QRect: The image area in viewer coordinates.
        """
        side = BUTTON_SIZE + 2 * MARGIN
        bottom = 20 + 2 * MARGIN if len(self.paths) > 1 else side
        return self.rect().adjusted(side, side, -side, -bottom)

    def _show_index(self, index: int) -> None:
        """Load and show one screenshot.

        Args:
            index (int): The screenshot's position, wrapped around the ends.
        """
        if not self.paths:
            return
        self.index = index % len(self.paths)
        self.pixmap = QPixmap(self.paths[self.index])
        self.scaled = QPixmap()
        self.counter.setText(f"{self.index + 1} / {len(self.paths)}")
        self.update()

    def _step(self, offset: int) -> None:
        """Show the previous or next screenshot.

        Args:
            offset (int): -1 for the previous screenshot, 1 for the next.
        """
        if len(self.paths) > 1:
            self._show_index(self.index + offset)
