from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from core.model_catalog import ModelOption
from ui.input.selection_dropdown import SelectionDropdown
from ui.theme import DANGER_COLOR, PRIMARY_COLOR, qcolor, qss


class InputSettings(QWidget):
    """Display matching model and effort selectors below the message input."""

    model_changed = pyqtSignal(str)
    effort_changed = pyqtSignal(str)
    reload_requested = pyqtSignal()
    directory_requested = pyqtSignal()
    directory_cleared = pyqtSignal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.models: dict[str, ModelOption] = {}
        self.model_dropdown = SelectionDropdown([("Loading models...", "")], "")
        self.model_dropdown.setEnabled(False)
        self.effort_dropdown = SelectionDropdown([("Default", "default")], "default")
        self.effort_dropdown.hide()
        self.model_dropdown.setAccessibleName("Model")
        self.effort_dropdown.setAccessibleName("Reasoning effort")
        self.record_button = _RecordButton()
        self.set_recording(False)
        self.directory_widget = QWidget()
        self.directory_widget.hide()
        self.directory_button = QPushButton()
        self.directory_button.setAccessibleName("Claude directory")
        self.clear_directory_button = QPushButton("×")
        self.clear_directory_button.setAccessibleName("Use no directory")
        self.clear_directory_button.setToolTip(
            "Use no directory so Claude sees no project files"
        )
        self._init_UI()

    def set_models(
        self, models: tuple[ModelOption, ...], model_id: str = "", effort: str = ""
    ) -> None:
        """Populate model choices after background discovery completes.

        Args:
            models (tuple[ModelOption, ...]): Compatible model choices.
            model_id (str): The model to select when applying saved defaults.
            effort (str): The effort to select when applying saved defaults.
        """
        self.models = {model.model_id: model for model in models}
        self.model_dropdown.setToolTip("")
        self.model_dropdown.setEnabled(True)
        self.model_dropdown.set_options(
            [(model.display_name, model.model_id) for model in models]
        )
        if model_id:
            self.model_dropdown.set_value(model_id)
        else:
            self._on_model_changed(self.model_dropdown.current_value)
        if effort:
            self.effort_dropdown.set_value(effort)

    def set_directory(self, directory: str) -> None:
        """Show the three innermost directories Claude works in.

        Args:
            directory (str): The full directory path, or an empty string for no directory.
        """
        parts = Path(directory).parts
        # Eliding only the drive saves nothing, so keep paths of up to three folders whole
        label = f"…\\{Path(*parts[-3:])}" if len(parts) > 4 else directory
        # Long folder names would otherwise push the model controls out of the row
        self.directory_button.setText(
            QFontMetrics(self.directory_button.font()).elidedText(
                label, Qt.TextElideMode.ElideLeft, 280
            )
            or "No directory"
        )
        self.directory_button.setToolTip(
            f"Claude can read files in {directory}. Click to change."
            if directory
            else "Claude has no project files. Click to choose a directory."
        )
        self.clear_directory_button.setVisible(bool(directory))

    def set_recording(self, recording: bool) -> None:
        """Show whether audio is being recorded.

        Args:
            recording (bool): Whether recording is active.
        """
        self.record_button.setChecked(recording)
        self.record_button.setToolTip(
            "Stop recording audio (Right Ctrl+Shift+R)"
            if recording
            else "Record and transcribe your microphone and computer audio (Right Ctrl+Shift+R)"
        )
        self.record_button.update()

    def show_loading(self) -> None:
        """Clear stale model and effort choices during provider discovery."""
        self.close_popups()
        self.models = {}
        self.model_dropdown.blockSignals(True)
        self.model_dropdown.set_options([("Loading models…", "")])
        self.model_dropdown.blockSignals(False)
        self.model_dropdown.setEnabled(False)
        self.effort_dropdown.hide()

    def show_model_error(self, error: str) -> None:
        """Offer a retry when discovery fails without inventing fallback models.

        Args:
            error (str): The discovery error to show in the tooltip.
        """
        self.model_dropdown.blockSignals(True)
        self.model_dropdown.set_options([("Retry models", "")])
        self.model_dropdown.blockSignals(False)
        self.effort_dropdown.hide()
        self.model_dropdown.setToolTip(error)
        self.model_dropdown.setEnabled(True)

    def close_popups(self) -> None:
        """Close both settings popups before sending or hiding the overlay."""
        self.model_dropdown.close_popup()
        self.effort_dropdown.close_popup()

    def reposition_popups(self, delta: int) -> None:
        """Keep either popup aligned when the input field grows.

        Args:
            delta (int): The vertical movement in pixels.
        """
        self.model_dropdown.reposition_popup(delta)
        self.effort_dropdown.reposition_popup(delta)

    def _init_UI(self) -> None:
        """Initialize the settings row with matching translucent dropdowns."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.model_dropdown.value_changed.connect(self._on_model_changed)
        self.effort_dropdown.value_changed.connect(self.effort_changed)
        directory_layout = QHBoxLayout(self.directory_widget)
        directory_layout.setContentsMargins(
            6, 0, 0, 0
        )  # Line up with the text edit's left padding
        directory_layout.setSpacing(4)
        self.directory_widget.setStyleSheet(f"""
            QPushButton {{ color: {qss(PRIMARY_COLOR, 200)}; background: transparent; border: none; padding: 0; }}
            QPushButton:hover {{ color: {qss(PRIMARY_COLOR)}; }}
        """)
        for button in (self.directory_button, self.clear_directory_button):
            font = button.font()
            font.setPixelSize(12)
            button.setFont(font)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            directory_layout.addWidget(button)
        self.directory_button.clicked.connect(self.directory_requested)
        self.clear_directory_button.clicked.connect(self.directory_cleared)
        layout.addWidget(self.directory_widget)
        layout.addWidget(self.record_button)
        layout.addStretch()
        layout.addWidget(self.model_dropdown)
        layout.addWidget(self.effort_dropdown)

    def _on_model_changed(self, model_id: str) -> None:
        """Update the model and restrict effort choices to supported levels.

        Args:
            model_id (str): The selected Gemini model identifier.
        """
        if not model_id:
            self.model_dropdown.setEnabled(False)
            self.model_dropdown.set_options([("Loading models...", "")])
            self.reload_requested.emit()
            return
        self.model_changed.emit(model_id)
        self.effort_dropdown.set_options(
            [(level.title(), level) for level in self.models[model_id].effort_levels]
        )
        self.effort_dropdown.setToolTip(
            self.models[model_id].effort_warning
            or "Reasoning effort. Higher levels can take longer."
        )
        self.effort_dropdown.setVisible(
            self.models[model_id].effort_levels != ("default",)
        )


class _RecordButton(QPushButton):
    """Draw a microphone glyph that turns red while recording."""

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(24, 24)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Record audio")
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Paint the microphone in the idle, hover, or recording color."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked():
            color = qcolor(DANGER_COLOR)
        else:
            color = qcolor(PRIMARY_COLOR, 255 if self.underMouse() else 200)
        center = self.width() / 2
        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.isChecked():
            painter.setBrush(color)
        painter.drawRoundedRect(QRectF(center - 3, 4, 6, 10), 3, 3)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Cradle arc under the capsule, then the stand
        painter.drawArc(QRectF(center - 5.5, 6.5, 11, 11), 200 * 16, 140 * 16)
        painter.drawLine(QPointF(center, 17.5), QPointF(center, 20))
        painter.drawLine(QPointF(center - 3, 20), QPointF(center + 3, 20))
