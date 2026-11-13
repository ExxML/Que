import ctypes
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.claude_bridge import ClaudeBridge
from core.model_catalog import ModelOption
from core.provider_catalog import ProviderCatalog, ProviderResult
from core.settings import SettingsStore
from ui.theme import BG_COLOR, PRIMARY_COLOR, qss


class SettingsPage(QWidget):
    """Configure provider connections and independent model defaults inside the overlay."""

    saved = pyqtSignal(object)

    def __init__(
        self,
        settings_store: SettingsStore,
        claude_bridge: ClaudeBridge,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.settings_store = settings_store
        self.catalog = ProviderCatalog(claude_bridge)
        self.catalog.completed.connect(self._on_loaded)
        self.catalog.failed.connect(self._on_failed)
        self.revision = 0
        self.provider = settings_store.provider()
        self.models: tuple[ModelOption, ...] = ()
        self.drafts: dict[str, tuple[str, str]] = {}
        self.loading = False
        self.key_timer = QTimer(self)
        self.key_timer.setSingleShot(True)
        self.key_timer.setInterval(500)
        self.key_timer.timeout.connect(lambda: self._refresh(False))
        self._init_UI()

    def open_settings(self) -> None:
        """Load saved values when opening or reopening the settings page."""
        self.key_timer.stop()
        self.provider = self.settings_store.provider()
        self.models = ()
        self.drafts = {
            provider: self.settings_store.defaults(provider)
            for provider in ("gemini", "claude")
        }
        self.key_input.blockSignals(True)
        self.key_input.setText(self.settings_store.gemini_key())
        self.key_input.blockSignals(False)
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key.setChecked(False)
        self.gemini_button.setChecked(self.provider == "gemini")
        self.claude_button.setChecked(self.provider == "claude")
        self._update_provider()
        if self.settings_store.error:
            self.status.setText(self.settings_store.error)

    def _init_UI(self) -> None:
        """Build an accessible form that applies verified settings automatically."""
        # Styling the drop-down area replaces the native arrow, so supply one
        arrow_path = (
            Path(__file__).resolve().parents[2] / "assets" / "dropdown_arrow.svg"
        ).as_posix()
        self.setStyleSheet(f"""
            QLabel {{ color: {qss(PRIMARY_COLOR, 210)}; background: transparent; }}
            QLineEdit, QComboBox, QPushButton {{
                color: {qss(PRIMARY_COLOR)}; background: transparent;
                border: 1px solid {qss(PRIMARY_COLOR, 65)}; border-radius: 6px;
                padding: 7px 10px; min-height: 20px;
            }}
            QPushButton:hover, QPushButton:checked {{ background: {qss(PRIMARY_COLOR, 40)}; }}
            QComboBox::drop-down {{ background: transparent; border: none; width: 24px; }}
            QComboBox::down-arrow {{ image: url({arrow_path}); width: 8px; height: 5px; }}
            QPushButton:focus, QLineEdit:focus, QComboBox:focus {{ border-color: {qss(PRIMARY_COLOR, 200)}; }}
            QPushButton:disabled, QComboBox:disabled {{ color: {qss(PRIMARY_COLOR, 80)}; }}
            QComboBox QAbstractItemView {{ background: {qss(BG_COLOR)}; color: {qss(PRIMARY_COLOR)};
                selection-background-color: {qss(PRIMARY_COLOR, 50)}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 12, 22, 18)
        layout.setSpacing(12)
        title = QLabel("AI settings")
        font = title.font()
        font.setPointSize(17)
        title.setFont(font)
        layout.addWidget(title)
        description = QLabel("Choose your AI provider and the defaults for new chats.")
        description.setWordWrap(True)
        layout.addWidget(description)
        provider_row = QHBoxLayout()
        self.gemini_button = QPushButton("Gemini")
        self.claude_button = QPushButton("Claude")
        self.provider_group = QButtonGroup(self)
        for button in (self.gemini_button, self.claude_button):
            button.setCheckable(True)
            self.provider_group.addButton(button)
            provider_row.addWidget(button)
        self.gemini_button.clicked.connect(lambda: self._select_provider("gemini"))
        self.claude_button.clicked.connect(lambda: self._select_provider("claude"))
        layout.addLayout(provider_row)
        self.connection_hint = QLabel()
        self.connection_hint.setWordWrap(True)
        layout.addWidget(self.connection_hint)
        self.key_panel = QWidget()
        key_layout = QVBoxLayout(self.key_panel)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_label = QLabel("Gemini API key")
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("Paste your Gemini API key")
        self.key_input.setAccessibleName("Gemini API key")
        self.key_input.textChanged.connect(self._on_key_changed)
        key_label.setBuddy(self.key_input)
        key_layout.addWidget(key_label)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_input, 1)
        self.show_key = QPushButton("Show")
        self.show_key.setCheckable(True)
        self.show_key.toggled.connect(self._toggle_key)
        key_row.addWidget(self.show_key)
        key_layout.addLayout(key_row)
        key_layout.addWidget(
            QLabel("Saved encrypted for your Windows account. No .env needed.")
        )
        layout.addWidget(self.key_panel)
        check_row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh models")
        self.refresh_button.clicked.connect(lambda: self._refresh(False))
        self.test_button = QPushButton("Test key")
        self.test_button.setToolTip(
            "Send a tiny Gemini request to verify generation access. Uses API quota."
        )
        self.test_button.clicked.connect(lambda: self._refresh(True))
        check_row.addWidget(self.refresh_button)
        check_row.addWidget(self.test_button)
        check_row.addStretch()
        layout.addLayout(check_row)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Connection status")
        layout.addWidget(self.status)
        form = QFormLayout()
        self.model_combo = _SettingsComboBox()
        self.model_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.model_combo.setMinimumContentsLength(20)
        self.effort_combo = _SettingsComboBox()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.effort_combo.currentIndexChanged.connect(self._save)
        form.addRow("Default &model", self.model_combo)
        form.addRow("Default &effort", self.effort_combo)
        layout.addLayout(form)
        effort_hint = QLabel(
            "Higher effort can take longer. You can change model and effort below the chat input."
        )
        effort_hint.setWordWrap(True)
        layout.addWidget(effort_hint)
        context_hint = QLabel(
            "Each provider keeps its own conversation in this chat. New chat resets both."
        )
        context_hint.setWordWrap(True)
        layout.addWidget(context_hint)
        layout.addStretch(1)
        self.save_status = QLabel("Changes are saved automatically after validation.")
        self.save_status.setWordWrap(True)
        layout.addWidget(self.save_status)

    def _select_provider(self, provider: str) -> None:
        """Retain draft defaults when switching the provider toggle.

        Args:
            provider (str): The provider selected in the form.
        """
        if self.provider == provider:
            return
        if self.models:
            self.drafts[self.provider] = (
                str(self.model_combo.currentData()),
                str(self.effort_combo.currentData()),
            )
        self.provider = provider
        self.models = ()
        self.gemini_button.setChecked(provider == "gemini")
        self.claude_button.setChecked(provider == "claude")
        self._update_provider()

    def _update_provider(self) -> None:
        """Show only the selected provider's connection fields and capabilities."""
        gemini = self.provider == "gemini"
        self.key_panel.setVisible(gemini)
        self.test_button.setVisible(gemini)
        self.refresh_button.setText("Refresh models" if gemini else "Check connection")
        self.connection_hint.setText(
            "Connect with a key from Google AI Studio."
            if gemini
            else "Requires Claude Pro and Claude Code Terminal. Sign in there with claude auth login, then check the connection."
        )
        self._refresh(False)

    def _on_key_changed(self, _text: str) -> None:
        """Validate a new key automatically after a short typing pause."""
        self.revision = -1
        self._set_loading(False)
        self._clear_models()
        self.status.setText("Waiting to check the new key…")
        self.save_status.setText(
            "Your saved settings stay active until this key is verified."
        )
        self.key_timer.start()

    def _toggle_key(self, visible: bool) -> None:
        """Reveal or mask the API key on explicit request.

        Args:
            visible (bool): Whether to reveal the credential.
        """
        self.key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.show_key.setText("Hide" if visible else "Show")

    def _refresh(self, test_generation: bool) -> None:
        """Check the captured provider and credential in a background worker.

        Args:
            test_generation (bool): Whether to send a Gemini test request.
        """
        self.key_timer.stop()
        if self.models:
            self.drafts[self.provider] = (
                str(self.model_combo.currentData()),
                str(self.effort_combo.currentData()),
            )
        self._clear_models()
        if self.provider == "gemini" and not self.key_input.text().strip():
            self.revision = -1
            self._set_loading(False)
            self.status.setText("Paste a Gemini API key to get started.")
            return
        self._set_loading(True)
        self.save_status.setText(
            "Changes will apply automatically when the connection is verified."
        )
        self.status.setText(
            "Testing key…"
            if test_generation
            else "Checking connection and loading models…"
        )
        self.revision = self.catalog.refresh(
            self.provider, self.key_input.text().strip(), test_generation
        )

    def _clear_models(self) -> None:
        """Remove stale choices until the selected provider is verified."""
        self.models = ()
        self.model_combo.clear()
        self.effort_combo.clear()
        self.model_combo.setPlaceholderText("Connect to load models")
        self.effort_combo.setPlaceholderText("Select a model first")
        self.model_combo.setEnabled(False)
        self.effort_combo.setEnabled(False)

    def _set_loading(self, loading: bool) -> None:
        """Prevent duplicate checks while leaving provider switching available.

        Args:
            loading (bool): Whether a connection check is running.
        """
        self.loading = loading
        self.refresh_button.setEnabled(not loading)
        self.test_button.setEnabled(not loading)

    def _on_loaded(self, revision: int, result: ProviderResult) -> None:
        """Display only the latest connection check and restore valid defaults.

        Args:
            revision (int): The completed check identifier.
            result (ProviderResult): The provider catalog and status.
        """
        if revision != self.revision or result.provider != self.provider:
            return
        self._set_loading(False)
        self.models = result.models
        self.status.setText(result.status)
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model in self.models:
            self.model_combo.addItem(model.display_name, model.model_id)
        default_model, _ = self.drafts[self.provider]
        index = self.model_combo.findData(default_model)
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)
        self.model_combo.setEnabled(True)
        self._on_model_changed(self.model_combo.currentIndex())

    def _on_failed(self, revision: int, error: str) -> None:
        """Show actionable connection failures without exposing stale choices.

        Args:
            revision (int): The completed check identifier.
            error (str): The redacted error message.
        """
        if revision != self.revision:
            return
        self._set_loading(False)
        self.status.setText(error)
        self.save_status.setText(
            "Changes were not applied. Your saved settings are still active."
        )

    def _on_model_changed(self, index: int) -> None:
        """Restrict effort choices to the current model's discovered capabilities.

        Args:
            index (int): The selected model index.
        """
        previous = self.effort_combo.currentData() or self.drafts[self.provider][1]
        self.effort_combo.blockSignals(True)
        try:
            self.effort_combo.clear()
            if not 0 <= index < len(self.models):
                return
            for level in self.models[index].effort_levels:
                self.effort_combo.addItem(level.title(), level)
            self.effort_combo.setCurrentIndex(
                max(0, self.effort_combo.findData(previous))
            )
            self.effort_combo.setEnabled(self.effort_combo.count() > 1)
        finally:
            self.effort_combo.blockSignals(False)
        self._save()

    def _save(self) -> None:
        """Persist changed, verified settings without interrupting page navigation."""
        if not self.models or self.loading:
            return
        model = self.model_combo.currentData()
        effort = self.effort_combo.currentData()
        if model is None or effort is None:
            return
        saved_key = self.settings_store.gemini_key()
        api_key = (
            self.key_input.text().strip() if self.provider == "gemini" else saved_key
        )
        if (
            self.provider == self.settings_store.provider()
            and (model, effort) == self.settings_store.defaults(self.provider)
            and api_key == saved_key
            and not self.settings_store.error
        ):
            self.save_status.setText("Settings are up to date.")
            return
        try:
            self.settings_store.save(
                self.provider,
                str(model),
                str(effort),
                api_key,
            )
        except Exception:
            self.save_status.setText(
                "Changes could not be saved. Check that Que's data folder is writable, then refresh to retry."
            )
            return
        self.drafts[self.provider] = (str(model), str(effort))
        self.save_status.setText("Changes saved automatically.")
        self.saved.emit(ProviderResult(self.provider, self.models, self.status.text()))


class _SettingsComboBox(QComboBox):
    """Exclude native settings popups from Windows screen capture."""

    def showPopup(self) -> None:
        """Apply the overlay's capture exclusion before showing the popup."""
        view = self.view()
        if view is not None:
            popup = view.window()
            if popup is not None:
                ctypes.windll.user32.SetWindowDisplayAffinity(int(popup.winId()), 0x11)
        super().showPopup()
