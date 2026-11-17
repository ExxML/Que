from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QEnterEvent,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from ui.theme import BG_COLOR, PRIMARY_COLOR, qcolor, qss

ITEM_HORIZONTAL_PADDING = 8
ITEM_VERTICAL_PADDING = 6
SCROLLBAR_WIDTH = 5


class SelectionDropdown(QWidget):
    """Select a value with a custom dropdown button."""

    value_changed = pyqtSignal(str)

    def __init__(
        self,
        options: list[tuple[str, str]],
        current_value: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.options = options
        self.current_value = current_value
        self.current_display = next(
            name for name, value in options if value == current_value
        )
        self.popup: _SelectionPopup | None = None
        self.shield: _PopupShield | None = None
        self._init_UI()

    def close_popup(self) -> None:
        """Close the selection popup if it is visible."""
        if self.popup is not None:
            self.popup.close()

    def set_options(self, options: list[tuple[str, str]]) -> None:
        """Update options and retain the selection when it is still supported.

        Args:
            options (list[tuple[str, str]]): Nonempty display labels and values.
        """
        self.close_popup()
        self.options = options
        values = [value for _, value in options]
        value = self.current_value if self.current_value in values else values[0]
        display = next(name for name, option in options if option == value)
        changed = value != self.current_value
        self.current_value = value
        self.current_display = display
        self._update_size()
        self.update()
        if changed:
            self.value_changed.emit(value)

    def set_value(self, value: str) -> None:
        """Select an available value and notify connected controls.

        Args:
            value (str): The option identifier to select.
        """
        display = next((name for name, option in self.options if option == value), None)
        if display is not None:
            self.close_popup()
            self._on_option_selected(display, value)

    def reposition_popup(self, delta: int) -> None:
        """Shift the popup and shield vertically to keep them aligned.

        Args:
            delta (int): The number of pixels to move the popup downward.
        """
        if self.popup is None or not self.popup.isVisible():
            return

        new_geometry = self.popup.geometry().translated(0, delta)
        self.popup.setGeometry(new_geometry)
        if self.shield is not None:
            self.shield.setGeometry(new_geometry)

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """Close the popup on an outside click or window deactivation.

        Args:
            watched (QObject): The object being filtered.
            event (QEvent): The event being processed.

        Returns:
            bool: Return whether the base class handled the event.
        """
        if event is not None and self.popup is not None:
            if (
                event.type() == QEvent.Type.KeyPress
                and isinstance(event, QKeyEvent)
                and event.key() == Qt.Key.Key_Escape
            ):
                self.close_popup()
                return True
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(
                event, QMouseEvent
            ):
                gp = event.globalPosition().toPoint()
                if not self.popup.rect().contains(
                    self.popup.mapFromGlobal(gp)
                ) and not self.rect().contains(self.mapFromGlobal(gp)):
                    self.popup.close()
            elif event.type() == QEvent.Type.WindowDeactivate:
                self.popup.close()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        """Toggle the selection popup on click.

        Args:
            event (QMouseEvent): The mouse press event that triggered the toggle.
        """
        if event is None or event.button() != Qt.MouseButton.LeftButton:
            return

        if self.popup is not None and self.popup.isVisible():
            self.popup.close()
            return

        self._show_popup()

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Draw the current option label right-aligned."""
        painter = QPainter(self)
        painter.setPen(qcolor(PRIMARY_COLOR, 128))
        painter.setFont(self.font())
        painter.drawText(
            QRect(0, 0, self.width(), self.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self.current_display,
        )
        painter.end()

    def _init_UI(self) -> None:
        """Set up widget dimensions and cursor."""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        font = self.font()
        font.setPixelSize(12)
        self.setFont(font)
        self._update_size()

    def _update_size(self) -> None:
        """Fit the selected label so adjacent controls follow its width."""
        fm = QFontMetrics(self.font())
        self.setFixedWidth(fm.horizontalAdvance(self.current_display))
        self.setFixedHeight(fm.height() + 8)  # Arbitrary vertical padding

    def _show_popup(self) -> None:
        """Create and display the selection popup above this widget."""
        main_window = self.window()
        assert main_window is not None

        global_pos = self.mapToGlobal(QPoint(self.width(), 0))
        local_pos = main_window.mapFromGlobal(global_pos)

        self.popup = _SelectionPopup(main_window, self.options, self.current_value)
        self.popup.option_selected.connect(self._on_option_selected)
        self.popup.closed.connect(self._on_popup_closed)
        popup_size = self.popup.size()
        popup_size.setHeight(min(popup_size.height(), max(60, local_pos.y())))
        self.popup.setFixedHeight(popup_size.height())
        self.popup.setGeometry(
            local_pos.x() - popup_size.width(),
            local_pos.y() - popup_size.height(),
            popup_size.width(),
            popup_size.height(),
        )

        self.shield = _PopupShield(main_window)
        self.shield.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.shield.setGeometry(self.popup.geometry())
        self.shield.raise_()
        self.shield.show()

        self.popup.raise_()
        self.popup.show()
        app = QApplication.instance()
        assert app is not None
        app.installEventFilter(self)

    def _on_option_selected(self, display_name: str, value: str) -> None:
        """Handle a selection from the popup.

        Args:
            display_name (str): The option label shown in the dropdown.
            value (str): The option identifier.
        """
        self.current_display = display_name
        self.current_value = value
        self._update_size()
        self.update()
        self.value_changed.emit(value)

    def _on_popup_closed(self) -> None:
        """Clean up the shield, event filter, and popup references."""
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self.shield is not None:
            self.shield.close()
            self.shield = None
        self.popup = None


class _SelectionPopup(QWidget):
    """Display a frameless popup for selecting an option."""

    option_selected = pyqtSignal(str, str)
    closed = pyqtSignal()

    def __init__(
        self, parent: QWidget, options: list[tuple[str, str]], current_value: str
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.options = options
        self._init_UI(current_value)

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Emit the closed signal when the popup is closing.

        Args:
            event (QCloseEvent): The close event passed to the base widget.
        """
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        """Close the popup on Escape key press.

        Args:
            event (QKeyEvent): The key event being processed.
        """
        if event is not None and event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Draw the popup background with rounded corners and a border."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)

        painter.fillPath(path, qcolor(BG_COLOR, 153))
        painter.strokePath(path, QPen(qcolor(PRIMARY_COLOR, 128), 1))
        painter.end()

    def _init_UI(self, current_value: str) -> None:
        """Build the list of options.

        Args:
            current_value (str): The identifier of the currently selected option.
        """
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(1, 1, 1, 1)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setStyleSheet(f"""
            QScrollArea, QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: {SCROLLBAR_WIDTH}px;
                border: none;
                margin: 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background-color: {qss(PRIMARY_COLOR, 77)};
                border-radius: 2px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: {qss(PRIMARY_COLOR, 128)};
            }}
            QScrollBar::handle:vertical:disabled {{
                background: transparent;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
                background: none;
                border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)
        items = QWidget()
        layout = QVBoxLayout(items)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.scroll_area.setWidget(items)
        outer_layout.addWidget(self.scroll_area)

        font = self.font()
        font.setPixelSize(12)
        fm = QFontMetrics(font)
        item_width = (
            max(fm.horizontalAdvance(name) for name, _ in self.options)
            + ITEM_HORIZONTAL_PADDING * 2
        )

        for display_name, value in self.options:
            item = _SelectionItem(display_name, value, value == current_value)
            item.setFixedWidth(item_width)
            item.clicked.connect(self._on_item_clicked)
            layout.addWidget(item)
        self.setFixedWidth(item_width + SCROLLBAR_WIDTH + 2)
        self.setFixedHeight(min(items.sizeHint().height() + 2, 280))

    def _on_item_clicked(self, display_name: str, value: str) -> None:
        """Forward the selection and close the popup.

        Args:
            display_name (str): The option label selected by the user.
            value (str): The option identifier selected by the user.
        """
        self.option_selected.emit(display_name, value)
        self.close()


class _PopupShield(QWidget):
    """Clear the main window content behind the popup."""

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Erase the backing store pixels to transparent with rounded corners."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)

        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 9, 9)
        painter.fillPath(path, Qt.GlobalColor.transparent)
        painter.end()


class _SelectionItem(QWidget):
    """Render an individual option within the selection popup."""

    clicked = pyqtSignal(str, str)

    def __init__(self, display_name: str, value: str, is_selected: bool) -> None:
        super().__init__()
        self.display_name = display_name
        self.value = value
        self.is_selected = is_selected
        self.is_hovered = False
        self._init_UI()

    def enterEvent(self, _event: QEnterEvent | None) -> None:
        """Highlight the item on mouse hover."""
        self.is_hovered = True
        self.update()

    def leaveEvent(self, _event: QEvent | None) -> None:
        """Remove highlight when mouse leaves."""
        self.is_hovered = False
        self.update()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        """Emit clicked signal with the option label and value."""
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.display_name, self.value)

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Draw the option label with color based on selection and hover state."""
        painter = QPainter(self)

        if self.is_hovered:
            # Paint semi-transparent background on hover
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            offset = self.mapToParent(QPoint(0, 0))
            parent = self.parentWidget()
            assert parent is not None
            popup_rect = QRectF(
                -offset.x(), -offset.y(), parent.width(), parent.height()
            )
            path = QPainterPath()
            path.addRoundedRect(popup_rect, 8, 8)
            painter.fillPath(path, qcolor(PRIMARY_COLOR, 25))
            # Set text colour to max opacity
            painter.setPen(qcolor(PRIMARY_COLOR, 255))
        else:
            # Set text colour to lower opacity
            painter.setPen(qcolor(PRIMARY_COLOR, 255 if self.is_selected else 190))

        painter.setFont(self.font())
        text_rect = QRect(
            ITEM_HORIZONTAL_PADDING,
            0,
            self.width() - ITEM_HORIZONTAL_PADDING * 2,
            self.height(),
        )
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.display_name,
        )
        painter.end()

    def _init_UI(self) -> None:
        """Set up the item dimensions and cursor."""
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        font = self.font()
        font.setPixelSize(12)
        self.setFont(font)

        fm = QFontMetrics(font)
        self.setFixedHeight(fm.height() + ITEM_VERTICAL_PADDING * 2)
