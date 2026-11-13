from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from core.model_catalog import ModelOption
from ui.input.selection_dropdown import SelectionDropdown


class InputSettings(QWidget):
    """Display matching model and effort selectors below the message input."""

    model_changed = pyqtSignal(str)
    effort_changed = pyqtSignal(str)
    reload_requested = pyqtSignal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.models: dict[str, ModelOption] = {}
        self.model_dropdown = SelectionDropdown([("Loading models...", "")], "")
        self.model_dropdown.setEnabled(False)
        self.effort_dropdown = SelectionDropdown([("Default", "default")], "default")
        self.effort_dropdown.hide()
        self.model_dropdown.setAccessibleName("Model")
        self.effort_dropdown.setAccessibleName("Reasoning effort")
        self.effort_dropdown.setToolTip(
            "Reasoning effort. Higher levels can take longer."
        )
        self._init_UI()

    def set_models(self, models: tuple[ModelOption, ...]) -> None:
        """Populate model choices after background discovery completes.

        Args:
            models (tuple[ModelOption, ...]): Compatible model choices.
        """
        self.models = {model.model_id: model for model in models}
        self.model_dropdown.setToolTip("")
        self.model_dropdown.setEnabled(True)
        self.model_dropdown.set_options(
            [(model.display_name, model.model_id) for model in models]
        )

    def show_model_error(self, error: str) -> None:
        """Offer a retry when discovery fails without inventing fallback models.

        Args:
            error (str): The discovery error to show in the tooltip.
        """
        self.model_dropdown.set_options([("Retry models", "")])
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
        self.effort_dropdown.setVisible(
            self.models[model_id].effort_levels != ("default",)
        )
