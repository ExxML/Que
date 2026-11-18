import time
from dataclasses import dataclass

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import DANGER_COLOR, PRIMARY_COLOR, WARNING_COLOR, qcolor, qss

ICON = "⚠"  # Drawn with the monochrome symbol font, so it takes the severity color
MAX_ENTRIES = 6  # shortcut: oldest one-off events drop first, so a leak storm cannot grow the window without bound


@dataclass
class BannerEntry:
    """Hold one warning's severity, lifetime, and display state."""

    critical: bool  # Protection is off, not only reduced
    lasting: bool  # Holds until resolved, so dismissing only hides it
    dismissed: bool
    time: str


class StatusBanner(QWidget):
    """Show Que's protection warnings in a framed strip below the title bar.

    Warnings are kept apart from the chat so they never read as AI text. The strip is
    docked in the window layout and has no fill of its own. Dismissing a warning that
    still holds leaves a small badge in the title bar that shows it again.
    """

    size_changed = pyqtSignal()

    def __init__(self, main_window: QWidget) -> None:
        super().__init__(main_window)
        self.entries: dict[str, BannerEntry] = {}  # Keyed by text, oldest first
        self.badge = QPushButton(ICON, main_window)
        self.frame = QWidget()
        self.rows = QVBoxLayout(self.frame)
        self.row_font = QFont("Microsoft JhengHei", 9)
        self.icon_font = QFont(
            "Segoe UI Symbol", 9
        )  # The default fallback draws the sign as a yellow emoji
        self.border_color = WARNING_COLOR
        self._init_UI()
        self._refresh()

    def wanted(self) -> bool:
        """Report whether any warning is shown.

        Returns:
            bool: True while at least one warning is not dismissed.
        """
        return any(not entry.dismissed for entry in self.entries.values())

    def show_warning(
        self, text: str, critical: bool = False, lasting: bool = True
    ) -> None:
        """Show a warning, or refresh the time of one already listed.

        A listed warning keeps its dismissed state, so a condition reported again
        does not reappear after the user hid it.

        Args:
            text (str): The warning.
            critical (bool): Whether protection is off rather than reduced.
            lasting (bool): Whether the warning holds until resolved instead of being a one-off event.
        """
        now = time.strftime("%I:%M %p").lstrip("0")
        entry = self.entries.get(text)
        if entry is None:
            self.entries[text] = BannerEntry(critical, lasting, False, now)
            self._trim()
        else:
            entry.critical = entry.critical or critical
            entry.time = now
        self._refresh()

    def clear_warning(self, text: str) -> None:
        """Remove a warning that no longer holds.

        Args:
            text (str): The warning.
        """
        if self.entries.pop(text, None) is not None:
            self._refresh()

    def show_all(self) -> None:
        """Show every dismissed warning again."""
        for entry in self.entries.values():
            entry.dismissed = False
        self._refresh()

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Outline the warnings in the color of the most severe one shown."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(qcolor(self.border_color, 200), 1))
        # No fill: a translucent fill would stack on the window's tint
        painter.drawRoundedRect(
            QRectF(self.frame.geometry()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10
        )

    def _init_UI(self) -> None:
        """Build the framed strip and the title bar badge."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 4, 12, 4)
        self.frame.setStyleSheet(f"""
            QLabel {{ background: transparent; }}
            QPushButton {{ color: {qss(PRIMARY_COLOR, 160)}; background: transparent; border: none; padding: 0 4px; }}
            QPushButton:hover {{ color: {qss(PRIMARY_COLOR)}; }}
        """)
        outer.addWidget(self.frame)
        self.rows.setContentsMargins(10, 6, 4, 6)
        self.rows.setSpacing(4)

        self.badge.setFixedSize(24, 32)
        self.badge.setFont(self.icon_font)
        self.badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.badge.setAccessibleName("Protection warnings")
        self.badge.clicked.connect(self.show_all)
        self.setVisible(False)

    def _trim(self) -> None:
        """Drop the oldest one-off events beyond the entry limit."""
        events = [text for text, entry in self.entries.items() if not entry.lasting]
        while len(self.entries) > MAX_ENTRIES and events:
            del self.entries[events.pop(0)]

    def _dismiss(self, text: str) -> None:
        """Hide a warning, forgetting it if it was a one-off event.

        Args:
            text (str): The warning.
        """
        entry = self.entries[text]
        if entry.lasting:
            entry.dismissed = True
        else:
            del self.entries[text]
        self._refresh()

    def _refresh(self) -> None:
        """Rebuild the warning rows, border color, and badge, then report the size change."""
        while self.rows.count():
            item = self.rows.takeAt(0)
            assert item is not None
            row = item.widget()
            if row is not None:
                row.hide()  # Deletion is deferred, so hide the row now
                row.deleteLater()
        shown = {
            text: entry for text, entry in self.entries.items() if not entry.dismissed
        }
        for text, entry in shown.items():
            row = self._row(text, entry)
            self.rows.addWidget(row)
            row.show()
        # Layouts show new rows and refresh cached sizes on a later event-loop pass, and never
        # while hidden, but the window reads the banner's height now
        self.frame.updateGeometry()
        self.border_color = (
            DANGER_COLOR
            if any(entry.critical for entry in shown.values())
            else WARNING_COLOR
        )
        self.update()

        hidden = [entry for entry in self.entries.values() if entry.dismissed]
        badge_color = (
            DANGER_COLOR if any(entry.critical for entry in hidden) else WARNING_COLOR
        )
        self.badge.setVisible(bool(hidden))
        self.badge.setToolTip(
            f"{len(hidden)} hidden protection warning{'s' if len(hidden) != 1 else ''}. Click to show."
        )
        # Dim until hovered so a known condition stays mostly out of sight
        self.badge.setStyleSheet(f"""
            QPushButton {{ color: {qss(badge_color, 110)}; background: transparent; border: none; }}
            QPushButton:hover {{ color: {qss(badge_color)}; }}
        """)
        self.size_changed.emit()

    def _row(self, text: str, entry: BannerEntry) -> QWidget:
        """Build one warning row: a severity icon, the text, its time, and a dismiss button.

        Args:
            text (str): The warning.
            entry (BannerEntry): The warning's state.

        Returns:
            QWidget: The row.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        icon = QLabel(ICON)
        icon.setStyleSheet(
            f"color: {qss(DANGER_COLOR if entry.critical else WARNING_COLOR)};"
        )
        message = QLabel(text)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        message.setStyleSheet(f"color: {qss(PRIMARY_COLOR, 235)};")
        when = QLabel(entry.time)
        when.setStyleSheet(f"color: {qss(PRIMARY_COLOR, 140)};")
        dismiss = QPushButton("×")
        dismiss.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        dismiss.setAccessibleName("Dismiss warning")
        dismiss.setToolTip("Dismiss")
        dismiss.clicked.connect(lambda: self._dismiss(text))
        for widget in (icon, message, when, dismiss):
            widget.setFont(self.icon_font if widget is icon else self.row_font)
            layout.addWidget(
                widget, 1 if widget is message else 0, Qt.AlignmentFlag.AlignTop
            )
        return row
