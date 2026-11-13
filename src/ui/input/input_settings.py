from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from core.ai_sender import DEFAULT_EFFORT, DEFAULT_MODEL, EFFORT_LEVELS, MODELS
from ui.input.selection_dropdown import SelectionDropdown


class InputSettings(QWidget):
    """Display matching model and effort selectors below the message input."""

    model_changed = pyqtSignal(str)
    effort_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.model_dropdown = SelectionDropdown(MODELS, DEFAULT_MODEL)
        self.effort_dropdown = SelectionDropdown(
            [(level.title(), level) for level in EFFORT_LEVELS[DEFAULT_MODEL]],
            DEFAULT_EFFORT,
            prefix="Effort: ",
        )
        self.model_dropdown.setAccessibleName("Model")
        self.effort_dropdown.setAccessibleName("Reasoning effort")
        self.effort_dropdown.setToolTip(
            "Reasoning effort. Higher levels can take longer."
        )
        self._init_UI()

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

        layout.addStretch()
        layout.addWidget(self.model_dropdown)
        layout.addWidget(self.effort_dropdown)

    def _on_model_changed(self, model_id: str) -> None:
        """Update the model and restrict effort choices to supported levels.

        Args:
            model_id (str): The selected Gemini model identifier.
        """
        self.model_changed.emit(model_id)
        self.effort_dropdown.set_options(
            [(level.title(), level) for level in EFFORT_LEVELS[model_id]]
        )
