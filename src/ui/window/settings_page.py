import ctypes
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtBoundSignal, pyqtSignal
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from core.claude_bridge import ClaudeBridge
from core.model_catalog import ModelOption
from core.provider_catalog import ProviderCatalog, ProviderResult
from core.settings import WHISPER_MODELS, SettingsStore
from core.transcriber import Transcriber
from ui.chat.chat_area import SCROLLBAR_STYLE
from ui.directory_picker import choose_directory
from ui.theme import BG_COLOR, PRIMARY_COLOR, qss

WHISPER_LABELS: dict[str, str] = {
    "tiny.en": "Tiny · fastest · 75 MB",
    "base.en": "Base · fast · 145 MB",
    "small.en": "Small · recommended · 480 MB",
    "medium.en": "Medium · most accurate · 1.5 GB",
}


class SettingsPage(QWidget):
    """Configure provider connections and independent model defaults inside the overlay."""

    saved = pyqtSignal(object)
    directory_saved = pyqtSignal(str)
    system_tray_saved = pyqtSignal(bool)
    capture_hidden_saved = pyqtSignal(bool)
    click_through_saved = pyqtSignal(bool)

    def __init__(
        self,
        settings_store: SettingsStore,
        claude_bridge: ClaudeBridge,
        transcriber: Transcriber,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.settings_store = settings_store
        self.transcriber = transcriber
        transcriber.download_finished.connect(self._on_whisper_downloaded)
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
        self._show_directory(self.settings_store.claude_directory())
        self.gemini_button.setChecked(self.provider == "gemini")
        self.claude_button.setChecked(self.provider == "claude")
        self._select_whisper(
            self.transcriber.downloading
            or self.transcriber.active_model()
            or "small.en"
        )
        self.tray_checkbox.blockSignals(True)
        self.tray_checkbox.setChecked(self.settings_store.system_tray())
        self.tray_checkbox.blockSignals(False)
        self._update_provider()
        if self.settings_store.error:
            self.status.setText(self.settings_store.error)

    def _init_UI(self) -> None:
        """Build accessible, tabbed forms that apply verified settings automatically."""
        # Styling the drop-down area replaces the native arrow, so supply one
        arrow_path = (
            Path(__file__).resolve().parents[2] / "assets" / "dropdown_arrow.svg"
        ).as_posix()
        self.setStyleSheet(f"""
            QLabel, QCheckBox {{ color: {qss(PRIMARY_COLOR, 210)}; background: transparent; }}
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
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QFrame#tab_row {{ background: transparent; border-bottom: 1px solid {qss(PRIMARY_COLOR, 40)}; }}
            QTabBar::tab {{
                color: {qss(PRIMARY_COLOR, 150)}; background: transparent; border: none;
                border-bottom: 2px solid transparent; padding: 6px 2px; margin-right: 20px;
            }}
            QTabBar::tab:hover {{ color: {qss(PRIMARY_COLOR, 220)}; }}
            QTabBar::tab:selected {{ color: {qss(PRIMARY_COLOR)}; border-bottom-color: {qss(PRIMARY_COLOR)}; }}
        """)
        # Only the tab contents scroll, so the title, tabs, and save status stay visible
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 12, 0, 18)
        page_layout.setSpacing(8)
        title = QLabel("Settings")
        font = title.font()
        font.setPointSize(17)
        title.setFont(font)
        title.setContentsMargins(22, 0, 22, 0)
        page_layout.addWidget(title)
        tab_row = QFrame()
        tab_row.setObjectName("tab_row")
        tab_row_layout = QHBoxLayout(tab_row)
        tab_row_layout.setContentsMargins(22, 0, 22, 0)
        self.tab_bar = QTabBar()
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setAccessibleName("Settings sections")
        tab_row_layout.addWidget(self.tab_bar)
        tab_row_layout.addStretch()
        page_layout.addWidget(tab_row)
        self.tab_pages = QStackedWidget()
        self.tab_bar.currentChanged.connect(self.tab_pages.setCurrentIndex)
        page_layout.addWidget(self.tab_pages, 1)
        layout = self._add_tab("&AI")
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
        self.directory_panel = QWidget()
        directory_layout = QVBoxLayout(self.directory_panel)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_label = QLabel("Default directory")
        self.directory_input = QLineEdit()
        self.directory_input.setReadOnly(True)
        self.directory_input.setPlaceholderText("No directory")
        self.directory_input.setAccessibleName("Default Claude directory")
        directory_label.setBuddy(self.directory_input)
        directory_layout.addWidget(directory_label)
        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory_input, 1)
        self.directory_button = QPushButton("Browse…")
        self.directory_button.clicked.connect(self._choose_directory)
        directory_row.addWidget(self.directory_button)
        self.no_directory_button = QPushButton("Use none")
        self.no_directory_button.setToolTip(
            "Start Claude with no directory so it sees no project files"
        )
        self.no_directory_button.clicked.connect(lambda: self._save_directory(""))
        directory_row.addWidget(self.no_directory_button)
        directory_layout.addLayout(directory_row)
        directory_hint = QLabel(
            "Claude starts here with read-only access to its files. With no directory, it sees no project files. Change it for this session below the chat input."
        )
        directory_hint.setWordWrap(True)
        directory_layout.addWidget(directory_hint)
        layout.addWidget(self.directory_panel)
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
        voice_layout = self._add_tab("&Voice")
        transcription_form = QFormLayout()
        self.whisper_combo = _SettingsComboBox()
        for model in WHISPER_MODELS:
            self.whisper_combo.addItem(WHISPER_LABELS[model], model)
        self.whisper_combo.currentIndexChanged.connect(self._on_whisper_changed)
        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self._download_whisper)
        transcription_row = QHBoxLayout()
        transcription_row.addWidget(self.whisper_combo, 1)
        transcription_row.addWidget(self.download_button)
        transcription_label = QLabel("&Transcription")
        transcription_label.setBuddy(self.whisper_combo)
        transcription_form.addRow(transcription_label, transcription_row)
        voice_layout.addLayout(transcription_form)
        self.download_status = QLabel()
        self.download_status.setTextFormat(Qt.TextFormat.PlainText)
        self.download_status.setWordWrap(True)
        self.download_status.setAccessibleName("Transcription model download status")
        self.download_status.hide()
        voice_layout.addWidget(self.download_status)
        transcription_hint = QLabel(
            "Audio is transcribed on this computer and never uploaded. Download a model once to use it. Larger models are more accurate but slower. Applies the next time you start recording (Ctrl+Shift+R)."
        )
        transcription_hint.setWordWrap(True)
        voice_layout.addWidget(transcription_hint)
        voice_layout.addStretch(1)
        window_layout = self._add_tab("&Window")
        self.tray_checkbox = QCheckBox("Show system tray &icon")
        self.tray_checkbox.toggled.connect(
            lambda enabled: self._save_toggle(
                self.settings_store.save_system_tray,
                self.system_tray_saved,
                "tray",
                enabled,
            )
        )
        self._add_toggle(
            window_layout,
            self.tray_checkbox,
            "Without the tray icon, show or hide Que with its hotkey and quit with the close button or quit hotkey.",
        )
        # Checked before connecting so the saved state is shown without re-saving it
        self.capture_checkbox = QCheckBox("Hide from screen &recording")
        self.capture_checkbox.setChecked(self.settings_store.capture_hidden())
        self.capture_checkbox.toggled.connect(
            lambda hidden: self._save_toggle(
                self.settings_store.save_capture_hidden,
                self.capture_hidden_saved,
                "screen recording",
                hidden,
            )
        )
        self._add_toggle(
            window_layout,
            self.capture_checkbox,
            "Screenshots, recordings, and screen sharing don't show Que. When off, Que also appears in its own screenshots.",
        )
        self.click_through_checkbox = QCheckBox("&Click through window")
        self.click_through_checkbox.setChecked(self.settings_store.click_through())
        self.click_through_checkbox.toggled.connect(
            lambda enabled: self._save_toggle(
                self.settings_store.save_click_through,
                self.click_through_saved,
                "click-through",
                enabled,
            )
        )
        self._add_toggle(
            window_layout,
            self.click_through_checkbox,
            "Mouse clicks pass through Que to the windows behind it, so you can't click Que itself. Press Ctrl+Shift+X to turn click-through on or off.",
        )
        window_layout.addStretch(1)
        self.save_status = QLabel("Changes are saved automatically after validation.")
        self.save_status.setWordWrap(True)
        self.save_status.setContentsMargins(22, 0, 22, 0)
        page_layout.addWidget(self.save_status)

    def _add_tab(self, name: str) -> QVBoxLayout:
        """Add a tab whose contents scroll independently below the tab bar.

        Args:
            name (str): The tab label, with an optional mnemonic.

        Returns:
            QVBoxLayout: The layout to fill with the tab's settings.
        """
        content = QWidget()
        tab_layout = QVBoxLayout(content)
        tab_layout.setContentsMargins(22, 14, 22, 8)
        tab_layout.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        # Styled on the bar itself so combo popups inside settings keep their own look
        scrollbar = scroll.verticalScrollBar()
        assert scrollbar is not None
        scrollbar.setStyleSheet(SCROLLBAR_STYLE)
        scroll.setWidget(content)
        self.tab_pages.addWidget(scroll)
        self.tab_bar.addTab(name)
        return tab_layout

    def _add_toggle(self, layout: QVBoxLayout, checkbox: QCheckBox, hint: str) -> None:
        """Add a checkbox with its explanation grouped closely below it.

        Args:
            layout (QVBoxLayout): The tab layout to add the setting to.
            checkbox (QCheckBox): The checkbox that toggles the setting.
            hint (str): The explanation of what the setting does.
        """
        group = QVBoxLayout()
        group.setSpacing(4)
        group.addWidget(checkbox)
        hint_label = QLabel(hint)
        hint_label.setWordWrap(True)
        group.addWidget(hint_label)
        checkbox.setAccessibleDescription(hint)
        layout.addLayout(group)

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
        self.directory_panel.setVisible(not gemini)
        self.test_button.setVisible(gemini)
        self.refresh_button.setText("Refresh models" if gemini else "Check connection")
        self.connection_hint.setText(
            "Connect with a key from Google AI Studio."
            if gemini
            else "Requires Claude Pro and Claude Code Terminal. Sign in there with claude auth login, then check the connection."
        )
        self._refresh(False)

    def _choose_directory(self) -> None:
        """Ask for a new default directory."""
        directory = choose_directory(
            self, self.directory_input.text() or str(Path.home())
        )
        if directory:
            self._save_directory(directory)

    def _save_directory(self, directory: str) -> None:
        """Save the default directory and apply it to the current session.

        Args:
            directory (str): The directory, or an empty string for no directory.
        """
        try:
            self.settings_store.save_claude_directory(directory)
        except OSError:
            self.save_status.setText(
                "The directory could not be saved. Check that Que's data folder is writable."
            )
            return
        self._show_directory(directory)
        self.save_status.setText("Changes saved automatically.")
        self.directory_saved.emit(directory)

    def _show_directory(self, directory: str) -> None:
        """Display the default directory and offer to clear it only when one is set.

        Args:
            directory (str): The directory, or an empty string for no directory.
        """
        self.directory_input.setText(directory)
        self.no_directory_button.setEnabled(bool(directory))

    def _select_whisper(self, model: str) -> None:
        """Show a transcription model as selected without saving it.

        Args:
            model (str): The faster-whisper model name.
        """
        self.whisper_combo.blockSignals(True)
        self.whisper_combo.setCurrentIndex(max(0, self.whisper_combo.findData(model)))
        self.whisper_combo.blockSignals(False)
        self._refresh_whisper()

    def _refresh_whisper(self) -> None:
        """Mark downloaded models and offer to download the selected one when missing."""
        downloaded = self.transcriber.downloaded_models()
        for index, model in enumerate(WHISPER_MODELS):
            self.whisper_combo.setItemText(
                index,
                f"{WHISPER_LABELS[model]} · downloaded"
                if model in downloaded
                else WHISPER_LABELS[model],
            )
        selected = self.whisper_combo.currentData() in downloaded
        downloading = bool(self.transcriber.downloading)
        self.download_button.setEnabled(not selected and not downloading)
        self.download_button.setText(
            "Downloading…" if downloading else "Downloaded" if selected else "Download"
        )

    def _on_whisper_changed(self, index: int) -> None:
        """Save a downloaded model, or explain that the selected model must be downloaded.

        Args:
            index (int): The selected model index.
        """
        model = str(self.whisper_combo.itemData(index))
        self._refresh_whisper()
        if model in self.transcriber.downloaded_models():
            self._save_whisper_model(model)
            if not self.transcriber.downloading:
                self.download_status.hide()
            return
        if self.transcriber.downloading:
            return
        active = self.transcriber.active_model()
        self._show_download_status(
            f"Download this model to use it. Recording uses the {active} model until then."
            if active
            else "Download this model to start transcribing."
        )

    def _download_whisper(self) -> None:
        """Start downloading the selected model."""
        model = str(self.whisper_combo.currentData())
        self.transcriber.download(model)
        self._show_download_status(
            f"Downloading the {model} model. You can keep using Que meanwhile."
        )
        self._refresh_whisper()

    def _on_whisper_downloaded(self, model: str, error: str) -> None:
        """Report a finished download and select the new model for transcription.

        Args:
            model (str): The faster-whisper model name.
            error (str): The download error, or an empty string on success.
        """
        if error:
            self._show_download_status(
                f"Could not download the {model} model. Check your internet connection and try again. ({error})"
            )
        else:
            try:
                self.settings_store.save_whisper_model(model)
            except OSError:
                self._show_download_status(
                    f"Downloaded the {model} model, but it could not be selected. Check that Que's data folder is writable."
                )
            else:
                self._show_download_status(
                    f"Downloaded the {model} model and selected it for transcription."
                )
                self._select_whisper(model)
        self._refresh_whisper()

    def _show_download_status(self, text: str) -> None:
        """Show download progress or results below the transcription settings.

        Args:
            text (str): The status to show.
        """
        self.download_status.setText(text)
        self.download_status.show()

    def _save_whisper_model(self, model: str) -> None:
        """Save the chosen transcription model.

        Args:
            model (str): The faster-whisper model name.
        """
        try:
            self.settings_store.save_whisper_model(model)
        except OSError:
            self.save_status.setText(
                "The transcription model could not be saved. Check that Que's data folder is writable."
            )
            return
        self.save_status.setText("Changes saved automatically.")

    def _save_toggle(
        self,
        save: Callable[[bool], None],
        saved: pyqtBoundSignal,
        name: str,
        enabled: bool,
    ) -> None:
        """Save and apply an on/off setting.

        Args:
            save (Callable[[bool], None]): The settings store method that saves the value.
            saved (pyqtBoundSignal): The signal that applies the saved value.
            name (str): The setting's name for the error message.
            enabled (bool): Whether the setting is on.
        """
        try:
            save(enabled)
        except OSError:
            self.save_status.setText(
                f"The {name} setting could not be saved. Check that Que's data folder is writable."
            )
            return
        self.save_status.setText("Changes saved automatically.")
        saved.emit(enabled)

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
        except Exception:  # noqa: BLE001
            self.save_status.setText(
                "Changes could not be saved. Check that Que's data folder is writable, then refresh to retry."
            )
            return
        self.drafts[self.provider] = (str(model), str(effort))
        self.save_status.setText("Changes saved automatically.")
        self.saved.emit(ProviderResult(self.provider, self.models, self.status.text()))


class _SettingsComboBox(QComboBox):
    """Exclude native settings popups from Windows screen capture and ignore wheel selection."""

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        """Pass wheel scrolling to the settings page instead of changing the selection.

        Args:
            event (QWheelEvent): The wheel event to forward to the parent.
        """
        if event is not None:
            event.ignore()

    def showPopup(self) -> None:
        """Apply the overlay's capture exclusion before showing the popup."""
        view = self.view()
        if view is not None:
            popup = view.window()
            if popup is not None:
                ctypes.windll.user32.SetWindowDisplayAffinity(int(popup.winId()), 0x11)
        super().showPopup()
