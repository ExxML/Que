from PyQt6.QtCore import QPoint, QRectF
from PyQt6.QtGui import QColor, QPainter, QPaintEvent
from PyQt6.QtWidgets import QPushButton, QWidget

from ui.theme import PRIMARY_COLOR, qcolor


class HeaderIconButton(QPushButton):
    """Draw a menu or plus icon snapped to whole device pixels.

    Font glyphs land on fractional pixels at scaled displays, which blurs some strokes
    thinner than others. Every stroke here starts and ends on a device pixel, so all
    strokes share one thickness and the icon is centered exactly in the button.
    """

    def __init__(self, glyph: str, parent: QWidget) -> None:
        super().__init__("", parent)
        self.glyph = glyph  # "menu" or "plus"

    def paintEvent(self, event: QPaintEvent | None) -> None:
        """Paint the button background, then the pixel-aligned icon strokes.

        Args:
            event (QPaintEvent): The paint event.
        """
        super().paintEvent(event)
        painter = QPainter(self)
        dpr = self.devicePixelRatioF()
        origin = self.mapTo(self.window(), QPoint(0, 0))
        origin_x = origin.x() * dpr
        origin_y = origin.y() * dpr
        center_x = round(origin_x + self.width() * dpr / 2)
        center_y = round(origin_y + self.height() * dpr / 2)
        thickness = max(1, round(1.25 * dpr))
        # Match the hover and idle glyph colors of the other title bar buttons
        color = QColor(0, 0, 0, 153) if self.underMouse() else qcolor(PRIMARY_COLOR, 77)

        # Strokes as (x, y, width, height) in window device pixels; none may overlap
        # because a translucent color painted twice would look brighter
        if self.glyph == "menu":
            width = round(14 * dpr)
            step = round(4.5 * dpr)
            top = center_y - (2 * step + thickness) // 2
            strokes = [
                (center_x - width // 2, top + index * step, width, thickness)
                for index in range(3)
            ]
        else:
            # Match the 2.5px strokes of the bold 24px "–" and "×" title bar glyphs; round half up
            thickness = int(2.5 * dpr + 0.5)
            length = round(13 * dpr)
            length += (length - thickness) % 2  # Keep both bars centered on each other
            # Position from the button edges so odd sizes are off-center by at most half a pixel
            plus_left = round(origin_x + (self.width() * dpr - length) / 2)
            plus_top = round(origin_y + (self.height() * dpr - length) / 2)
            arm = (length - thickness) // 2
            bar_top = plus_top + arm
            left = plus_left + arm
            strokes = [
                (plus_left, bar_top, length, thickness),
                (left, bar_top - arm, thickness, arm),
                (left, bar_top + thickness, thickness, arm),
            ]

        for x, y, width, height in strokes:
            painter.fillRect(
                QRectF(
                    (x - origin_x) / dpr,
                    (y - origin_y) / dpr,
                    width / dpr,
                    height / dpr,
                ),
                color,
            )
