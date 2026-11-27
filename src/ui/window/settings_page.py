from collections import Counter
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QSignalBlocker, Qt, QTimer, pyqtBoundSignal, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QHideEvent, QStandardItemModel, QWheelEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.document_text import extract_text
from core.hook_engine import PRESET_COUNT
from core.model_catalog import ModelOption
from core.provider_catalog import ProviderCatalog, ProviderResult
from core.resume_facts import ResumeFacts, facts_directory
from core.settings import (
    CONTEXT_DOCUMENTS,
    SPEECH_SPEED_MAX,
    SPEECH_SPEED_MIN,
    WHISPER_MODELS,
    SettingsStore,
)
from core.speaker import Speaker
from core.transcriber import Transcriber
from ui.chat.chat_area import SCROLLBAR_STYLE
from ui.directory_picker import choose_directory, choose_file
from ui.theme import BG_COLOR, DANGER_COLOR, PRIMARY_COLOR, qss
from ui.window.config_panel import ConfigPanel

WHISPER_LABELS: dict[str, str] = {
    "parakeet-tdt-0.6b-v2": "Parakeet TDT 0.6B · English · 670 MB",
}


class SettingsPage(QWidget):
    """Configure provider connections and independent model defaults inside the overlay."""

    saved = pyqtSignal(object)
    config_ai_saved = pyqtSignal(object)
    directory_saved = pyqtSignal(str)
    system_tray_saved = pyqtSignal(bool)
    capture_hidden_saved = pyqtSignal(bool)
    click_through_saved = pyqtSignal(bool)
    elevation_saved = pyqtSignal(bool)  # Applies at the next launch, so nothing listens
    speech_autoscroll_saved = pyqtSignal(bool)
    speech_highlight_saved = pyqtSignal(bool)

    def __init__(
        self,
        settings_store: SettingsStore,
        claude_bridge: ClaudeBridge,
        transcriber: Transcriber,
        chat_history: ChatHistory,
        parent: QWidget,
        speaker: Speaker | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings_store = settings_store
        self.chat_history = chat_history
        self.screenshots_armed = False
        self.transcriber = transcriber
        self.speaker = speaker or Speaker(settings_store)
        transcriber.download_finished.connect(self._on_whisper_downloaded)
        self.catalog = ProviderCatalog(claude_bridge)
        self.resume_facts = ResumeFacts(claude_bridge, facts_directory(settings_store))
        self.resume_facts.progress.connect(self._show_fact_sheets)
        self.catalog.completed.connect(self._on_loaded)
        self.catalog.failed.connect(self._on_failed)
        self.revision = 0
        self.provider = settings_store.provider()
        self.models: tuple[ModelOption, ...] = ()
        self.drafts: dict[str, tuple[str, str]] = {}
        self.loading = False
        self.restoring_config = False
        self.keep_voice_preference = False
        self.key_timer = QTimer(self)
        self.key_timer.setSingleShot(True)
        self.key_timer.setInterval(500)
        self.key_timer.timeout.connect(lambda: self._refresh(False))
        self.context_text_timer = QTimer(self)
        self.context_text_timer.setSingleShot(True)
        self.context_text_timer.setInterval(500)
        self.context_text_timer.timeout.connect(self._save_context_text)
        self.details_timer = QTimer(self)
        self.details_timer.setSingleShot(True)
        self.details_timer.setInterval(500)
        self.details_timer.timeout.connect(self._save_detail_names)
        self.detail_rows: list[tuple[_ResumeDetailNameEdit, str]] = []
        self.audio_devices: dict[str, list[str]] = {}
        self._init_UI()

    def open_settings(self) -> None:
        """Load saved values when opening or reopening the settings page."""
        self._select_whisper(
            self.transcriber.downloading
            or (
                self.settings_store.whisper_model()
                if self.keep_voice_preference
                else ""
            )
            or self.transcriber.active_model()
            or WHISPER_MODELS[0]
        )
        self._refresh_voice_buttons()
        self._refresh_speech_volume()
        self._refresh_speech_speed()
        self.tray_checkbox.blockSignals(True)
        self.tray_checkbox.setChecked(self.settings_store.system_tray())
        self.tray_checkbox.blockSignals(False)
        self.instruction_input.blockSignals(True)
        self.instruction_input.setPlainText(self.settings_store.instruction())
        self.instruction_input.blockSignals(False)
        with QSignalBlocker(self.job_details_input):
            self.job_details_input.setPlainText(self.settings_store.job_details())
        self._show_job_details_count()
        self._show_documents()
        self._show_resume_details()
        self._show_fact_sheets()
        self._show_screenshots_size()
        self._on_settings_tab_changed(self.tab_bar.currentIndex())
        self._load_ai_settings()

    def hideEvent(self, event: QHideEvent | None) -> None:
        """Save pending instruction, job detail and resume detail edits when leaving settings or hiding Que.

        Args:
            event (QHideEvent | None): The hide event.
        """
        if self.context_text_timer.isActive():
            self._save_context_text()
        if self.details_timer.isActive():
            self._save_detail_names()
        super().hideEvent(event)

    def _init_UI(self) -> None:
        """Build accessible, tabbed forms that apply verified settings automatically."""
        # Styling the drop-down area replaces the native arrow, so supply one
        arrow_path = (
            Path(__file__).resolve().parents[2] / "assets" / "dropdown_arrow.svg"
        ).as_posix()
        self.setStyleSheet(f"""
            QLabel, QCheckBox {{ color: {qss(PRIMARY_COLOR, 235)}; background: transparent; }}
            QLineEdit, QPlainTextEdit, QComboBox, QPushButton {{
                color: {qss(PRIMARY_COLOR)}; background: transparent;
                border: 1px solid {qss(PRIMARY_COLOR, 140)}; border-radius: 6px;
                padding: 7px 10px; min-height: 20px;
            }}
            QPushButton:hover, QPushButton:checked {{ background: {qss(PRIMARY_COLOR, 40)}; }}
            QComboBox::drop-down {{ background: transparent; border: none; width: 24px; }}
            QComboBox::down-arrow {{ image: url({arrow_path}); width: 8px; height: 5px; }}
            QSlider {{ background: transparent; min-height: 24px; }}
            QSlider::groove:horizontal {{
                height: 4px; background: transparent;
                border: 1px solid {qss(PRIMARY_COLOR, 140)}; border-radius: 3px;
            }}
            QSlider::sub-page:horizontal {{ background: {qss(PRIMARY_COLOR, 140)}; border-radius: 2px; }}
            QSlider::handle:horizontal {{
                width: 12px; margin: -5px 0; border-radius: 7px;
                background: {qss(PRIMARY_COLOR, 235)}; border: 1px solid transparent;
            }}
            QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {{
                background: {qss(PRIMARY_COLOR)}; border-color: {qss(BG_COLOR)};
            }}
            QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {qss(PRIMARY_COLOR, 200)}; }}
            QPushButton:disabled, QComboBox:disabled {{ color: {qss(PRIMARY_COLOR, 80)}; }}
            QLineEdit[missing="true"] {{ color: {qss(DANGER_COLOR)}; border-color: {qss(DANGER_COLOR, 160)}; }}
            QPushButton[armed="true"] {{
                color: {qss(DANGER_COLOR)}; background: {qss(DANGER_COLOR, 40)}; border-color: {qss(DANGER_COLOR, 160)};
            }}
            QComboBox QAbstractItemView {{ background: {qss(BG_COLOR)}; color: {qss(PRIMARY_COLOR)};
                selection-background-color: {qss(PRIMARY_COLOR, 50)}; }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QFrame#tab_row {{ background: transparent; border-bottom: 1px solid {qss(PRIMARY_COLOR, 40)}; }}
            QTabBar::tab {{
                color: {qss(PRIMARY_COLOR, 210)}; background: transparent; border: none;
                border-bottom: 2px solid transparent; padding: 6px 2px; margin-right: 16px;
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
        layout = self._add_tab("AI")
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
        form.addRow("Default model", self.model_combo)
        form.addRow("Default effort", self.effort_combo)
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
        presets_label = QLabel("Presets")
        presets_description = QLabel(
            "Press Caps Lock+1 to Caps Lock+0 to switch this chat to a model and effort, such as a fast model for short answers and a high effort for detailed ones. New chat returns to the defaults above."
        )
        presets_description.setWordWrap(True)
        presets_form = QFormLayout()
        self.preset_rows: list[tuple[QComboBox, QComboBox]] = []
        for slot in range(1, PRESET_COUNT + 1):
            shortcut = f"Caps Lock+{slot % 10}"
            preset_model = _SettingsComboBox()
            preset_model.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            preset_model.setMinimumContentsLength(14)
            preset_model.setAccessibleName(f"{shortcut} preset model")
            preset_effort = _SettingsComboBox()
            preset_effort.setAccessibleName(f"{shortcut} preset effort")
            row = len(self.preset_rows)
            preset_model.currentIndexChanged.connect(
                lambda _index, row=row: self._on_preset_model_changed(row)
            )
            preset_effort.currentIndexChanged.connect(self._save_presets)
            preset_row = QHBoxLayout()
            preset_row.addWidget(preset_model, 3)
            preset_row.addWidget(preset_effort, 2)
            presets_form.addRow(shortcut, preset_row)
            self.preset_rows.append((preset_model, preset_effort))
        presets_group = QVBoxLayout()
        presets_group.setSpacing(4)
        presets_group.addWidget(presets_label)
        presets_group.addWidget(presets_description)
        presets_group.addLayout(presets_form)
        layout.addLayout(presets_group)
        layout.addStretch(1)
        documents_layout = self._add_tab("Context")
        instruction_label = QLabel("Instruction")
        self.instruction_input = QPlainTextEdit()
        self.instruction_input.setPlaceholderText(
            "e.g., This is a practice online technical assessment for a job. Answer the questions concisely."
        )
        self.instruction_input.setAccessibleName("Instruction")
        self.instruction_input.setTabChangesFocus(True)
        self.instruction_input.setFixedHeight(96)
        instruction_scrollbar = self.instruction_input.verticalScrollBar()
        assert instruction_scrollbar is not None
        instruction_scrollbar.setStyleSheet(SCROLLBAR_STYLE)
        self.instruction_input.textChanged.connect(self.context_text_timer.start)
        instruction_label.setBuddy(self.instruction_input)
        instruction_hint = QLabel(
            "Sent once at the start of each new chat to tell the AI what the conversation is for, so changes apply to your next new chat."
        )
        instruction_hint.setWordWrap(True)
        self.instruction_input.setAccessibleDescription(instruction_hint.text())
        instruction_group = QVBoxLayout()
        instruction_group.setSpacing(4)
        instruction_group.addWidget(instruction_label)
        instruction_group.addWidget(self.instruction_input)
        instruction_group.addWidget(instruction_hint)
        documents_layout.addLayout(instruction_group)
        job_details_label = QLabel("Job and interview details")
        self.job_details_input = QPlainTextEdit()
        self.job_details_input.setPlaceholderText(
            "Paste the job description, plus anything else that helps, such as the company, team, interviewers, and interview format."
        )
        self.job_details_input.setAccessibleName("Job and interview details")
        self.job_details_input.setTabChangesFocus(True)
        self.job_details_input.setFixedHeight(160)
        job_details_scrollbar = self.job_details_input.verticalScrollBar()
        assert job_details_scrollbar is not None
        job_details_scrollbar.setStyleSheet(SCROLLBAR_STYLE)
        self.job_details_input.textChanged.connect(self.context_text_timer.start)
        self.job_details_input.textChanged.connect(self._show_job_details_count)
        job_details_label.setBuddy(self.job_details_input)
        self.job_details_count = QLabel()
        self.job_details_count.hide()
        job_details_hint = QLabel(
            "Sent once at the start of each new chat so answers fit the role, so changes apply to your next new chat."
        )
        job_details_hint.setWordWrap(True)
        self.job_details_input.setAccessibleDescription(job_details_hint.text())
        job_details_group = QVBoxLayout()
        job_details_group.setSpacing(4)
        job_details_group.addWidget(job_details_label)
        job_details_group.addWidget(self.job_details_input)
        job_details_group.addWidget(self.job_details_count)
        job_details_group.addWidget(job_details_hint)
        documents_layout.addLayout(job_details_group)
        documents_description = QLabel(
            "Add your resume, cover letter, and interview script so answers fit your experience, like during an interview."
        )
        documents_description.setWordWrap(True)
        documents_layout.addWidget(documents_description)
        self.document_names: dict[str, QLineEdit] = {}
        self.document_remove_buttons: dict[str, QPushButton] = {}
        for kind, label in CONTEXT_DOCUMENTS.items():
            document_label = QLabel(label)
            document_name = QLineEdit()
            document_name.setReadOnly(True)
            document_name.setPlaceholderText("No file")
            document_name.setAccessibleName(label)
            document_label.setBuddy(document_name)
            upload_button = QPushButton("Upload…")
            upload_button.setAccessibleName(f"Upload {label.lower()}")
            upload_button.clicked.connect(
                lambda _checked, kind=kind: self._upload_document(kind)
            )
            remove_button = QPushButton("Remove")
            remove_button.setAccessibleName(f"Remove {label.lower()}")
            remove_button.clicked.connect(
                lambda _checked, kind=kind: self._save_document(kind, "", "")
            )
            document_row = QHBoxLayout()
            document_row.addWidget(document_name, 1)
            document_row.addWidget(upload_button)
            document_row.addWidget(remove_button)
            document_group = QVBoxLayout()
            document_group.setSpacing(4)
            document_group.addWidget(document_label)
            document_group.addLayout(document_row)
            documents_layout.addLayout(document_group)
            self.document_names[kind] = document_name
            self.document_remove_buttons[kind] = remove_button
        documents_hint = QLabel(
            "PDF, Word (.docx), or text files. Que saves only their text on this computer and sends it once at the start of each new chat, so changes apply to your next new chat."
        )
        documents_hint.setWordWrap(True)
        documents_layout.addWidget(documents_hint)
        details_label = QLabel("Resume details")
        details_description = QLabel(
            "Link resume items to notes or project folders so the AI can explain them in depth. For the link name, use the full bullet or a key phrase, your role and employer, or the project or activity name. Include relevant technologies you used so the AI can easily look it up."
        )
        details_description.setWordWrap(True)
        # Rows sit in a plain widget so rebuilding them never disturbs the buttons below
        details_list = QWidget()
        self.details_list = QVBoxLayout(details_list)
        self.details_list.setContentsMargins(0, 0, 0, 0)
        self.details_list.setSpacing(6)
        link_file_button = QPushButton("Link file…")
        link_file_button.setAccessibleName("Link a file to a resume item")
        link_file_button.clicked.connect(lambda: self._link_resume_detail(False))
        link_folder_button = QPushButton("Link folder…")
        link_folder_button.setAccessibleName("Link a folder to a resume item")
        link_folder_button.clicked.connect(lambda: self._link_resume_detail(True))
        self.fact_sheets_button = QPushButton("Update fact sheets")
        self.fact_sheets_button.setAccessibleName(
            "Write fact sheets for new or changed linked folders"
        )
        self.fact_sheets_button.clicked.connect(self._update_fact_sheets)
        link_row = QHBoxLayout()
        link_row.addWidget(link_file_button)
        link_row.addWidget(link_folder_button)
        link_row.addWidget(self.fact_sheets_button)
        link_row.addStretch(1)
        self.fact_sheets_status = QLabel()
        self.fact_sheets_status.setWordWrap(True)
        details_hint = QLabel(
            "Files (PDF, Word, or text) are read again at the start of each new chat, so edits apply to your next new chat. When you link a folder, Claude reads it once and writes a fact sheet that new chats start from, so answers rarely wait on file reads. Claude can still open linked folders, and Gemini uses the fact sheet or, without one, the folder's README."
        )
        details_hint.setWordWrap(True)
        details_group = QVBoxLayout()
        details_group.setSpacing(4)
        details_group.addWidget(details_label)
        details_group.addWidget(details_description)
        details_group.addWidget(details_list)
        details_group.addLayout(link_row)
        details_group.addWidget(self.fact_sheets_status)
        details_group.addWidget(details_hint)
        documents_layout.addLayout(details_group)
        documents_layout.addStretch(1)
        voice_layout = self._add_tab("Voice")
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
        transcription_label = QLabel("Transcription")
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
        self._init_speech_settings(voice_layout)
        voice_layout.addStretch(1)
        self._init_audio_tab()
        window_layout = self._add_tab("Window")
        self.tray_checkbox = QCheckBox("Show system tray icon")
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
        self.capture_checkbox = QCheckBox("Hide from screen recording")
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
        self.click_through_checkbox = QCheckBox("Click through window")
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
            "Mouse clicks pass through Que to the windows behind it, so you can't click Que itself. Press Caps Lock+Shift+X to turn click-through on or off.",
        )
        self.elevation_checkbox = QCheckBox("Allow shortcuts in admin apps")
        self.elevation_checkbox.setChecked(self.settings_store.elevate_helper())
        self.elevation_checkbox.toggled.connect(
            lambda enabled: self._save_toggle(
                self.settings_store.save_elevate_helper,
                self.elevation_saved,
                "admin apps",
                enabled,
            )
        )
        self._add_toggle(
            window_layout,
            self.elevation_checkbox,
            "Que asks for administrator permission when it starts, so its shortcuts work and keys stay hidden in apps running as administrator, such as Task Manager. The permission prompt takes focus from the active app, so start Que before opening other apps. Takes effect the next time Que starts.",
        )
        window_layout.addStretch(1)
        storage_layout = self._add_tab("Storage")
        self.screenshots_size_label = QLabel()
        self.screenshots_size_label.setTextFormat(Qt.TextFormat.PlainText)
        self.screenshots_size_label.setAccessibleName("Screenshot storage used")
        storage_layout.addWidget(self.screenshots_size_label)
        self.delete_screenshots_button = QPushButton()
        self.delete_screenshots_button.clicked.connect(self._delete_screenshots)
        delete_row = QHBoxLayout()
        delete_row.addWidget(self.delete_screenshots_button)
        delete_row.addStretch()
        storage_layout.addLayout(delete_row)
        storage_hint = QLabel(
            "Screenshots you send are saved so saved chats can show them. Deleting them frees storage. Saved chats keep their messages but show their screenshots as missing."
        )
        storage_hint.setWordWrap(True)
        storage_layout.addWidget(storage_hint)
        storage_layout.addStretch(1)
        self.config_tab_index = self.tab_bar.count()
        config_layout = self._add_tab("Config")
        self.config_panel = ConfigPanel(self.settings_store, self._flush_context_edits)
        self.config_panel.imported.connect(self._apply_imported_config)
        config_layout.addWidget(self.config_panel, 1)
        # Connected after the tabs exist; switching tabs cancels a pending delete and remeasures
        self.tab_bar.currentChanged.connect(
            lambda _index: self._show_screenshots_size()
        )
        self.tab_bar.currentChanged.connect(self._on_settings_tab_changed)
        self._show_screenshots_size()
        self.save_status = QLabel("Changes are saved automatically after validation.")
        self.save_status.setWordWrap(True)
        self.save_status.setContentsMargins(22, 0, 22, 0)
        page_layout.addWidget(self.save_status)

    def _load_ai_settings(self) -> None:
        """Reload the saved AI preferences and check the active connection."""
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
        self._update_provider()
        if self.settings_store.error:
            self.status.setText(self.settings_store.error)

    def _flush_context_edits(self) -> bool:
        """Persist buffered Context edits before reading or writing a backup.

        Returns:
            bool: True if every Context field is up to date.
        """
        text_saved = self._save_context_text()
        details_saved = self._save_detail_names()
        return text_saved and details_saved

    def _apply_imported_config(self, selected: set[str]) -> None:
        """Update only controls and runtime consumers affected by the import.

        Args:
            selected (set[str]): The imported preference identifiers.
        """
        store = self.settings_store
        ai_changes = selected & {"provider", "gemini_key", "gemini", "claude"}
        if ai_changes:
            self.restoring_config = True
            self._load_ai_settings()
            self.config_ai_saved.emit(ai_changes)
        if self.models and any(
            key.startswith(f"presets.{self.provider}.") for key in selected
        ):
            self._show_presets()
        if "claude_directory" in selected:
            directory = store.claude_directory()
            self._show_directory(directory)
            self.directory_saved.emit(directory)
        if "instruction" in selected:
            with QSignalBlocker(self.instruction_input):
                self.instruction_input.setPlainText(store.instruction())
        if "job_details" in selected:
            with QSignalBlocker(self.job_details_input):
                self.job_details_input.setPlainText(store.job_details())
            self._show_job_details_count()
        if any(key.startswith("context_documents.") for key in selected):
            self._show_documents()
        if "resume_details" in selected:
            self._show_resume_details()
            self._update_fact_sheets()
        elif "fact_sheets" in selected:
            self._show_fact_sheets()
        if "whisper_model" in selected:
            self.keep_voice_preference = True
            self._select_whisper(store.whisper_model())
            self._show_download_status(
                "Download the imported model if it is unavailable on this device."
            )
        if "speech_volume" in selected:
            self._refresh_speech_volume()
        if "speech_speed" in selected:
            self._refresh_speech_speed()
        if (
            selected & {"audio_device_you", "audio_device_them", "audio_device_speaker"}
            and self.audio_refresh.isEnabled()
        ):
            self._on_audio_devices_loaded(self.audio_devices, "")
        for key, checkbox, value, signal in (
            (
                "system_tray",
                self.tray_checkbox,
                store.system_tray(),
                self.system_tray_saved,
            ),
            (
                "capture_hidden",
                self.capture_checkbox,
                store.capture_hidden(),
                self.capture_hidden_saved,
            ),
            (
                "click_through",
                self.click_through_checkbox,
                store.click_through(),
                self.click_through_saved,
            ),
            (
                "elevate_helper",
                self.elevation_checkbox,
                store.elevate_helper(),
                self.elevation_saved,
            ),
            (
                "speech_autoscroll",
                self.autoscroll_checkbox,
                store.speech_autoscroll(),
                self.speech_autoscroll_saved,
            ),
            (
                "speech_highlight",
                self.highlight_checkbox,
                store.speech_highlight(),
                self.speech_highlight_saved,
            ),
        ):
            if key in selected:
                with QSignalBlocker(checkbox):
                    checkbox.setChecked(value)
                signal.emit(value)
        self.save_status.setText("Imported settings saved.")

    def _init_speech_settings(self, layout: QVBoxLayout) -> None:
        """Add the speech model download, playback sliders and sample to Voice settings.

        Args:
            layout (QVBoxLayout): The Voice tab layout.
        """
        self.voice_download_button = QPushButton("Download")
        self.voice_download_button.setAccessibleName("Download speech model")
        self.voice_download_button.clicked.connect(self._download_voice)
        row = QHBoxLayout()
        row.addWidget(QLabel("Que's voice · English · About 350 MB"), 1)
        row.addWidget(self.voice_download_button)
        form = QFormLayout()
        form.addRow("Speech", row)
        self._init_speech_volume(form)
        self._init_speech_speed(form)
        self.voice_test_button = QPushButton("Test voice")
        self.voice_test_button.setAccessibleName("Test voice")
        self.voice_test_button.clicked.connect(self.speaker.speak_sample)
        test_row = QHBoxLayout()
        test_row.addStretch()
        test_row.addWidget(self.voice_test_button)
        form.addRow("", test_row)
        layout.addLayout(form)
        self.voice_download_status = QLabel()
        self.voice_download_status.setTextFormat(Qt.TextFormat.PlainText)
        self.voice_download_status.setWordWrap(True)
        self.voice_download_status.setAccessibleName("Speech model download status")
        self.voice_download_status.hide()
        layout.addWidget(self.voice_download_status)
        hint = QLabel("Download Que's voice once to read replies aloud (Caps Lock+V).")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._init_speech_follow(layout)
        self.speaker.download_finished.connect(self._on_voice_downloaded)
        self._refresh_voice_buttons()

    def _init_speech_follow(self, layout: QVBoxLayout) -> None:
        """Add the autoscroll and word highlight toggles for reading aloud.

        Args:
            layout (QVBoxLayout): The Voice tab layout.
        """
        # Checked before connecting so the saved state is shown without re-saving it
        self.autoscroll_checkbox = QCheckBox("Autoscroll while reading aloud")
        self.autoscroll_checkbox.setChecked(self.settings_store.speech_autoscroll())
        self.autoscroll_checkbox.toggled.connect(
            lambda enabled: self._save_toggle(
                self.settings_store.save_speech_autoscroll,
                self.speech_autoscroll_saved,
                "autoscroll",
                enabled,
            )
        )
        self._add_toggle(
            layout,
            self.autoscroll_checkbox,
            "The chat scrolls to keep the line being read in view. If you scroll yourself, it holds your place until you resume or start another reading.",
        )
        self.highlight_checkbox = QCheckBox("Highlight words while reading aloud")
        self.highlight_checkbox.setChecked(self.settings_store.speech_highlight())
        self.highlight_checkbox.toggled.connect(
            lambda enabled: self._save_toggle(
                self.settings_store.save_speech_highlight,
                self.speech_highlight_saved,
                "word highlighting",
                enabled,
            )
        )
        self._add_toggle(
            layout,
            self.highlight_checkbox,
            "Colors each word in the reply as Que reads it.",
        )

    def _init_speech_volume(self, form: QFormLayout) -> None:
        """Add the playback volume slider below the speech model.

        Args:
            form (QFormLayout): The speech settings form.
        """
        self.speech_volume_slider = _SettingsSlider(Qt.Orientation.Horizontal)
        self.speech_volume_slider.setRange(0, 100)
        self.speech_volume_value = self._add_speech_slider(
            form, "Volume", self.speech_volume_slider
        )
        self._refresh_speech_volume()
        self.speech_volume_slider.sliderMoved.connect(self._show_speech_volume_value)
        self.speech_volume_slider.valueChanged.connect(self._save_speech_volume)

    def _init_speech_speed(self, form: QFormLayout) -> None:
        """Add the speech speed slider below the volume slider.

        Args:
            form (QFormLayout): The speech settings form.
        """
        self.speech_speed_slider = _SettingsSlider(Qt.Orientation.Horizontal)
        self.speech_speed_slider.setRange(SPEECH_SPEED_MIN, SPEECH_SPEED_MAX)
        self.speech_speed_value = self._add_speech_slider(
            form, "Speed", self.speech_speed_slider
        )
        self._refresh_speech_speed()
        self.speech_speed_slider.sliderMoved.connect(self._show_speech_speed_value)
        self.speech_speed_slider.valueChanged.connect(self._save_speech_speed)

    def _add_speech_slider(
        self, form: QFormLayout, name: str, slider: QSlider
    ) -> QLabel:
        """Add an aligned speech control with a label and room for its value.

        Args:
            form (QFormLayout): The speech settings form.
            name (str): The name shown beside the slider.
            slider (QSlider): The slider to add to the form.

        Returns:
            QLabel: The readout beside the slider.
        """
        label = QLabel(name)
        label.setBuddy(slider)
        slider.setAccessibleName(f"Speech {name.lower()}")
        slider.setTracking(False)
        value = QLabel()
        metrics = value.fontMetrics()
        value.setMinimumWidth(
            max(metrics.horizontalAdvance("100%"), metrics.horizontalAdvance("2.00×"))
        )
        value.setAlignment(Qt.AlignmentFlag.AlignRight)
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(value)
        form.addRow(label, row)
        return value

    def _init_audio_tab(self) -> None:
        """Build the two independent recording source selectors and the speech output selector."""
        self.audio_tab_index = self.tab_bar.count()
        layout = self._add_tab("Audio")
        self.audio_combos: dict[str, QComboBox] = {}
        for source, label, hint in (
            (
                "you",
                "You · Microphone",
                "Choose the microphone that picks up your voice.",
            ),
            (
                "them",
                "Them · Playback device",
                "Choose the speakers or headphones your call or app plays through.",
            ),
            (
                "speaker",
                "Speech output",
                "Choose where Que reads replies aloud (Caps Lock+V), such as your earbuds.",
            ),
        ):
            title = QLabel(label)
            combo = _SettingsComboBox()
            combo.setAccessibleName(label)
            combo.setAccessibleDescription(hint)
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(16)
            combo.addItem("Windows default", "")
            title.setBuddy(combo)
            description = QLabel(hint)
            description.setWordWrap(True)
            group = QVBoxLayout()
            group.setSpacing(4)
            group.addWidget(title)
            group.addWidget(combo)
            group.addWidget(description)
            layout.addLayout(group)
            self.audio_combos[source] = combo
            combo.currentIndexChanged.connect(
                lambda index, source=source: self._save_audio_device(source, index)
            )
        hint_label = QLabel(
            "Changes apply the next time you start recording or Que starts reading."
        )
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)
        self.audio_refresh = QPushButton("Refresh devices")
        self.audio_refresh.clicked.connect(self._refresh_audio_devices)
        layout.addWidget(self.audio_refresh, alignment=Qt.AlignmentFlag.AlignLeft)
        self.audio_status = QLabel()
        self.audio_status.setWordWrap(True)
        self.audio_status.setAccessibleName("Audio device status")
        self.audio_status.hide()
        layout.addWidget(self.audio_status)
        layout.addStretch(1)
        self.transcriber.audio_devices_loaded.connect(self._on_audio_devices_loaded)

    def _on_settings_tab_changed(self, index: int) -> None:
        """Refresh Audio devices and let Config show its own operation status.

        Args:
            index (int): The selected settings tab.
        """
        self.save_status.setVisible(index != self.config_tab_index)
        if index == self.audio_tab_index:
            self._refresh_audio_devices()

    def _refresh_audio_devices(self) -> None:
        """Refresh the source list once and show progress inline."""
        if not self.audio_refresh.isEnabled():
            return
        self.audio_refresh.setEnabled(False)
        for combo in self.audio_combos.values():
            combo.setEnabled(False)
        self.audio_status.setText("Looking for audio devices…")
        self.audio_status.show()
        self.transcriber.refresh_audio_devices()

    def _on_audio_devices_loaded(
        self, devices: dict[str, list[str]], error: str
    ) -> None:
        """Display available choices while keeping disconnected preferences visible.

        Args:
            devices (dict[str, list[str]]): The WASAPI device names by source.
            error (str): The refresh error, or an empty string on success.
        """
        if not error:
            self.audio_devices = devices
        duplicate_names = False
        for source, combo in self.audio_combos.items():
            counts = Counter(self.audio_devices.get(source, ()))
            with QSignalBlocker(combo):
                combo.clear()
                combo.addItem("Windows default", "")
                for name, count in sorted(
                    counts.items(), key=lambda item: item[0].casefold()
                ):
                    if count > 1:
                        duplicate_names = True
                        continue
                    combo.addItem(
                        name.removesuffix(" [Loopback]") if source == "them" else name,
                        name,
                    )
                saved = self.settings_store.audio_device(source)
                index = combo.findData(saved)
                if index < 0:
                    label = (
                        saved.removesuffix(" [Loopback]") if source == "them" else saved
                    )
                    combo.addItem(f"{label} (unavailable)", saved)
                    index = combo.count() - 1
                    model = combo.model()
                    assert isinstance(model, QStandardItemModel)
                    item = model.item(index)
                    assert item is not None
                    item.setEnabled(False)
                combo.setCurrentIndex(index)
            combo.setEnabled(True)
        if not error and duplicate_names:
            error = "Some devices share a name. Give them unique names in Windows Sound settings, then refresh."
        elif not error and not any(self.audio_devices.values()):
            error = "No audio devices found. Connect a device and refresh."
        self.audio_status.setText(error)
        self.audio_status.setVisible(bool(error))
        self.audio_refresh.setEnabled(True)

    def _save_audio_device(self, source: str, index: int) -> None:
        """Save one selection and restore the previous choice if the write fails.

        Args:
            source (str): The transcript source whose selection changed.
            index (int): The selected row in that source's device list.
        """
        combo = self.audio_combos[source]
        name = combo.itemData(index)
        saved = self.settings_store.audio_device(source)
        if not isinstance(name, str) or name == saved:
            return
        try:
            self.settings_store.save_audio_device(source, name)
        except OSError:
            with QSignalBlocker(combo):
                combo.setCurrentIndex(combo.findData(saved))
            self.save_status.setText(
                "The audio device could not be saved. Check that Que's data folder is writable."
            )
            return
        self.save_status.setText("Changes saved automatically.")

    def _add_tab(self, name: str) -> QVBoxLayout:
        """Add a tab whose contents scroll independently below the tab bar.

        Args:
            name (str): The tab label.

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
        self.restoring_config = False
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

    def _upload_document(self, kind: str) -> None:
        """Ask for a document and save its text as context for new chats.

        Args:
            kind (str): The document kind from CONTEXT_DOCUMENTS.
        """
        filename = choose_file(
            self,
            f"Choose your {CONTEXT_DOCUMENTS[kind].lower()}",
            str(Path.home()),
            "Documents (*.pdf *.docx *.txt *.md)",
        )
        if not filename:
            return
        path = Path(filename)
        try:
            text = extract_text(path)
        except ValueError as error:
            self.save_status.setText(str(error))
            return
        self._save_document(kind, path.name, text)

    def _save_document(self, kind: str, name: str, text: str) -> None:
        """Save a document's text, or remove the document when the text is empty.

        Args:
            kind (str): The document kind from CONTEXT_DOCUMENTS.
            name (str): The original file name to display.
            text (str): The extracted document text.
        """
        try:
            self.settings_store.save_context_document(kind, name, text)
        except OSError:
            self.save_status.setText(
                f"The {CONTEXT_DOCUMENTS[kind].lower()} could not be saved. Check that Que's data folder is writable."
            )
            return
        self._show_documents()
        self.save_status.setText(
            "Changes saved automatically. They apply to new chats."
        )

    def _save_context_text(self) -> bool:
        """Save the typed instruction and job details where they differ from the saved ones.

        Returns:
            bool: True if both fields are up to date.
        """
        self.context_text_timer.stop()
        saved = True
        for name, text_input, current, save in (
            (
                "instruction",
                self.instruction_input,
                self.settings_store.instruction,
                self.settings_store.save_instruction,
            ),
            (
                "job details",
                self.job_details_input,
                self.settings_store.job_details,
                self.settings_store.save_job_details,
            ),
        ):
            text = text_input.toPlainText().strip()
            if text == current():
                continue
            try:
                save(text)
            except OSError:
                self.save_status.setText(
                    f"The {name} could not be saved. Check that Que's data folder is writable."
                )
                saved = False
                continue
            if saved:  # Keeps an earlier field's failure message visible
                self.save_status.setText(
                    "Changes saved automatically. They apply to new chats."
                )
        return saved

    def _show_job_details_count(self) -> None:
        """Show how many words the job details hold, hiding the count when they are empty."""
        words = len(self.job_details_input.toPlainText().split())
        self.job_details_count.setText(f"{words:,} word{'' if words == 1 else 's'}")
        self.job_details_count.setVisible(bool(words))

    def _show_documents(self) -> None:
        """Display each saved document's name and size, and offer removal only when saved."""
        documents = self.settings_store.context_documents()
        for kind, document_name in self.document_names.items():
            name, text = documents.get(kind, ("", ""))
            document_name.setText(
                f"{name} · {len(text.split()):,} words" if text else ""
            )
            self.document_remove_buttons[kind].setEnabled(bool(text))

    def _link_resume_detail(self, folder: bool) -> None:
        """Ask for a file or folder that explains a resume item, then link it and focus its name.

        Args:
            folder (bool): Whether to link a folder instead of a file.
        """
        details = self._resume_details()
        current = str(Path(details[-1][1]).parent) if details else str(Path.home())
        if folder:
            path = choose_directory(
                self, current, "Choose the folder behind a resume item"
            )
        else:
            path = choose_file(
                self,
                "Choose notes about a resume item",
                current,
                "Documents (*.pdf *.docx *.txt *.md)",
            )
            if path:
                try:
                    extract_text(Path(path))
                except ValueError as error:
                    self.save_status.setText(str(error))
                    return
        if not path:
            return
        source = Path(path)
        name = source.name if folder else source.stem
        if not self._save_resume_details([*details, (name, path)]):
            return
        name_input = self.detail_rows[-1][0]
        name_input.setFocus()
        name_input.select_from_start()

    def _save_detail_names(self) -> bool:
        """Save typed resume item names when they differ from the saved ones."""
        self.details_timer.stop()
        details = self._resume_details()
        if details != self.settings_store.resume_details():
            return self._save_resume_details(details, False)
        return True

    def _save_resume_details(
        self, details: list[tuple[str, str]], rebuild: bool = True
    ) -> bool:
        """Save the resume detail links and show them.

        Args:
            details (list[tuple[str, str]]): The item name and file or folder path of each link.
            rebuild (bool): Whether the links were added or removed, so their rows must be rebuilt.

        Returns:
            bool: Whether the links were saved.
        """
        self.details_timer.stop()
        try:
            self.settings_store.save_resume_details(details)
        except OSError:
            self.save_status.setText(
                "The resume details could not be saved. Check that Que's data folder is writable."
            )
            return False
        if rebuild:
            self._show_resume_details()
            self._update_fact_sheets()
        self.save_status.setText(
            "Changes saved automatically. They apply to new chats."
        )
        return True

    def _update_fact_sheets(self) -> None:
        """Have Claude write fact sheets for linked folders that have none or changed since."""
        model, effort = self.settings_store.defaults("claude")
        self.resume_facts.refresh(self.settings_store.resume_details(), model, effort)
        self._show_fact_sheets()

    def _show_fact_sheets(self) -> None:
        """Show how many linked folders have a current fact sheet and offer to update the rest."""
        current, total, writing, error = self.resume_facts.status(
            self.settings_store.resume_details()
        )
        if not total:
            text = ""
        elif writing:
            text = (
                f"Writing fact sheets… {current} of {total} linked folders are ready."
            )
        else:
            text = f"Fact sheets: {current} of {total} linked folders are up to date."
            if error:
                text += f" The last one failed: {error}"
        self.fact_sheets_status.setText(text)
        self.fact_sheets_status.setVisible(bool(text))
        self.fact_sheets_button.setVisible(bool(total))
        self.fact_sheets_button.setEnabled(not writing and current < total)

    def _resume_details(self) -> list[tuple[str, str]]:
        """Return the resume detail links as currently shown, including unsaved name edits.

        Returns:
            list[tuple[str, str]]: The item name and file or folder path of each link.
        """
        return [
            (name_input.text().strip(), path) for name_input, path in self.detail_rows
        ]

    def _show_resume_details(self) -> None:
        """Rebuild one row per saved link, flagging files and folders that no longer exist."""
        while (item := self.details_list.takeAt(0)) is not None:
            if (widget := item.widget()) is not None:
                widget.deleteLater()
        self.detail_rows = []
        for index, (name, path) in enumerate(self.settings_store.resume_details()):
            source = Path(path)
            kind = "folder" if source.is_dir() else "file"
            name_input = _ResumeDetailNameEdit(name)
            name_input.setPlaceholderText("Resume item, e.g., Que overlay")
            name_input.setAccessibleName(f"Resume item linked to {source.name}")
            name_input.textChanged.connect(self.details_timer.start)
            path_input = QLineEdit(path)
            path_input.setReadOnly(True)
            path_input.setCursorPosition(0)
            path_input.setAccessibleName(f"Linked {kind}")
            path_input.setToolTip(path)
            if not source.exists():
                path_input.setProperty("missing", True)
                path_input.setToolTip(
                    f"{path} no longer exists. Remove this link, then link the file or folder again."
                )
            path_input.setAccessibleDescription(path_input.toolTip())
            remove_button = QPushButton("Remove")
            remove_button.setAccessibleName(f"Remove link to {source.name}")
            remove_button.clicked.connect(
                lambda _checked, index=index: self._save_resume_details(
                    [
                        detail
                        for position, detail in enumerate(self._resume_details())
                        if position != index
                    ]
                )
            )
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(name_input, 2)
            row_layout.addWidget(path_input, 3)
            row_layout.addWidget(remove_button)
            self.details_list.addWidget(row)
            self.detail_rows.append((name_input, path))

    def _show_screenshots_size(self) -> None:
        """Display the storage used by stored screenshots and reset the delete button."""
        size = self.chat_history.screenshots_size()
        self.screenshots_size_label.setText(f"Screenshots use {_format_size(size)}.")
        self._set_screenshots_armed(False)
        self.delete_screenshots_button.setEnabled(size > 0)

    def _delete_screenshots(self) -> None:
        """Ask for confirmation on the first click and delete every stored screenshot on the second."""
        if not self.screenshots_armed:
            self._set_screenshots_armed(True)
            return
        if self.chat_history.delete_screenshots():
            self.save_status.setText("Deleted all screenshots.")
        else:
            self.save_status.setText(
                "Some screenshots could not be deleted. Check that Que's data folder is writable."
            )
        self._show_screenshots_size()

    def _set_screenshots_armed(self, armed: bool) -> None:
        """Show the delete button in its confirming or normal state.

        Args:
            armed (bool): Whether the next click deletes every stored screenshot.
        """
        self.screenshots_armed = armed
        button = self.delete_screenshots_button
        button.setText(
            "Click again to delete permanently" if armed else "Delete all screenshots"
        )
        button.setToolTip(
            "This can't be undone. Switch tabs to cancel." if armed else ""
        )
        # A page rule keyed on this property restyles reliably; clearing a widget's own sheet left it red
        button.setProperty("armed", armed)
        style = button.style()
        assert style is not None
        style.unpolish(button)
        style.polish(button)

    def _select_whisper(self, model: str) -> None:
        """Show a transcription model as selected without saving it.

        Args:
            model (str): The transcription model name.
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
        self.keep_voice_preference = False
        model = str(self.whisper_combo.currentData())
        self.transcriber.download(model)
        self._show_download_status(
            f"Downloading the {model} model. You can keep using Que meanwhile."
        )
        self._refresh_whisper()

    def _on_whisper_downloaded(self, model: str, error: str) -> None:
        """Report a finished download and select the new model for transcription.

        Args:
            model (str): The transcription model name.
            error (str): The download error, or an empty string on success.
        """
        if error:
            self._show_download_status(
                f"Could not download the {model} model. Check your internet connection and try again. ({error})"
            )
        elif self.keep_voice_preference:
            self._show_download_status(f"Downloaded the {model} model.")
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

    def _refresh_voice_buttons(self) -> None:
        """Keep the download and sample buttons in sync with the cache and worker."""
        downloading = self.speaker.downloading
        downloaded = self.speaker.model_downloaded()
        self.voice_download_button.setEnabled(not downloading and not downloaded)
        self.voice_test_button.setEnabled(downloaded and not downloading)
        self.voice_download_button.setText(
            "Downloading…"
            if downloading
            else "Downloaded"
            if downloaded
            else "Download"
        )

    def _download_voice(self) -> None:
        """Start the speech download and report its progress in Voice settings."""
        if self.speaker.download():
            self.voice_download_status.setText(
                "Downloading Que's voice. You can keep using Que meanwhile."
            )
            self.voice_download_status.show()
        self._refresh_voice_buttons()

    def _on_voice_downloaded(self, error: str) -> None:
        """Report the speech model download result.

        Args:
            error (str): The download error, or an empty string on success.
        """
        self.voice_download_status.setText(
            f"Could not download Que's voice. Check your internet connection and try again. ({error})"
            if error
            else "Downloaded Que's voice."
        )
        self.voice_download_status.show()
        self._refresh_voice_buttons()

    def _refresh_speech_volume(self) -> None:
        """Restore the slider and percentage from the saved volume."""
        volume = self.settings_store.speech_volume()
        with QSignalBlocker(self.speech_volume_slider):
            self.speech_volume_slider.setValue(volume)
        self._show_speech_volume_value(volume)

    def _show_speech_volume_value(self, volume: int) -> None:
        """Show the slider's current percentage.

        Args:
            volume (int): The selected playback volume.
        """
        self.speech_volume_value.setText(f"{volume}%")

    def _save_speech_volume(self, volume: int) -> None:
        """Save the volume selection or restore the previous value on failure.

        Args:
            volume (int): The selected playback volume.
        """
        try:
            self.settings_store.save_speech_volume(volume)
        except OSError:
            self._refresh_speech_volume()
            self.save_status.setText(
                "The speech volume could not be saved. Check that Que's data folder is writable."
            )
            return
        self._show_speech_volume_value(volume)
        self.save_status.setText("Changes saved automatically.")

    def _refresh_speech_speed(self) -> None:
        """Restore the slider and multiplier from the saved speed."""
        speed = self.settings_store.speech_speed()
        with QSignalBlocker(self.speech_speed_slider):
            self.speech_speed_slider.setValue(speed)
        self._show_speech_speed_value(speed)

    def _show_speech_speed_value(self, speed: int) -> None:
        """Show the slider's speed multiplier.

        Args:
            speed (int): The selected speed as a percentage.
        """
        self.speech_speed_value.setText(f"{speed / 100:.2f}×")

    def _save_speech_speed(self, speed: int) -> None:
        """Save the speed selection or restore the previous value on failure.

        Args:
            speed (int): The selected speed as a percentage.
        """
        try:
            self.settings_store.save_speech_speed(speed)
        except OSError:
            self._refresh_speech_speed()
            self.save_status.setText(
                "The speech speed could not be saved. Check that Que's data folder is writable."
            )
            return
        self._show_speech_speed_value(speed)
        self.save_status.setText("Changes saved automatically.")

    def _save_whisper_model(self, model: str) -> None:
        """Save the chosen transcription model.

        Args:
            model (str): The transcription model name.
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
        self.restoring_config = False
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
        for preset_model, preset_effort in self.preset_rows:
            for combo in (preset_model, preset_effort):
                combo.blockSignals(True)
                combo.clear()
                combo.blockSignals(False)
                combo.setEnabled(False)
            preset_model.setPlaceholderText("Connect to load models")
            preset_effort.setPlaceholderText("Select a model first")

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
        self._show_presets()
        self._on_model_changed(
            self.model_combo.currentIndex(), save=not self.restoring_config
        )

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

    def _on_model_changed(self, index: int, *, save: bool = True) -> None:
        """Restrict effort choices to the current model's discovered capabilities.

        Args:
            index (int): The selected model index.
            save (bool): Whether to save the displayed choice as a new default.
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
        if save:
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

    def _show_presets(self) -> None:
        """Show the provider's saved presets, keeping any whose model is no longer offered."""
        presets = self.settings_store.presets(self.provider)
        for (preset_model, preset_effort), (model, effort) in zip(
            self.preset_rows, presets, strict=True
        ):
            preset_model.blockSignals(True)
            preset_model.clear()
            preset_model.addItem("Not set", "")
            for option in self.models:
                preset_model.addItem(option.display_name, option.model_id)
            if preset_model.findData(model) < 0:
                # Kept rather than shown as "Not set", so the shortcut's explanation matches
                preset_model.addItem(f"{model} · unavailable", model)
            preset_model.setCurrentIndex(preset_model.findData(model))
            preset_model.blockSignals(False)
            preset_model.setEnabled(True)
            self._show_preset_efforts(preset_model, preset_effort, effort)

    def _show_preset_efforts(
        self, preset_model: QComboBox, preset_effort: QComboBox, effort: str
    ) -> None:
        """Offer the efforts a preset's model supports, selecting the given one when supported.

        Args:
            preset_model (QComboBox): The preset's model selector.
            preset_effort (QComboBox): The preset's effort selector.
            effort (str): The effort to select.
        """
        model = preset_model.currentData()
        levels = next(
            (
                option.effort_levels
                for option in self.models
                if option.model_id == model
            ),
            (effort,) if model else (),
        )
        preset_effort.blockSignals(True)
        preset_effort.clear()
        for level in levels:
            preset_effort.addItem(level.title(), level)
        preset_effort.setCurrentIndex(max(0, preset_effort.findData(effort)))
        preset_effort.setEnabled(len(levels) > 1)
        preset_effort.blockSignals(False)

    def _on_preset_model_changed(self, row: int) -> None:
        """Restrict a preset's effort choices to its new model, then save the presets.

        Args:
            row (int): The preset's position, from 0 for Caps Lock+1.
        """
        preset_model, preset_effort = self.preset_rows[row]
        self._show_preset_efforts(
            preset_model, preset_effort, str(preset_effort.currentData() or "default")
        )
        self._save_presets()

    def _save_presets(self) -> None:
        """Persist changed presets for the provider shown."""
        if not self.models or self.loading:
            return
        presets = [
            (
                str(preset_model.currentData()),
                str(preset_effort.currentData() or "default"),
            )
            for preset_model, preset_effort in self.preset_rows
        ]
        if presets == self.settings_store.presets(self.provider):
            return
        try:
            self.settings_store.save_presets(self.provider, presets)
        except OSError:
            self.save_status.setText(
                "The presets could not be saved. Check that Que's data folder is writable."
            )
            return
        self.save_status.setText("Changes saved automatically.")


class _SettingsComboBox(QComboBox):
    """Exclude native settings popups from Windows screen capture and ignore wheel selection."""

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        """Pass wheel scrolling to the settings page instead of changing the selection.

        Args:
            event (QWheelEvent): The wheel event to forward to the parent.
        """
        if event is not None:
            event.ignore()


class _SettingsSlider(QSlider):
    """Keep scrolling the settings page from changing the slider value."""

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        """Pass the wheel event to the settings page.

        Args:
            event (QWheelEvent): The wheel event to forward to the parent.
        """
        if event is not None:
            event.ignore()


def _format_size(size: int) -> str:
    """Describe a file size in bytes with the largest fitting unit.

    Args:
        size (int): The size in bytes.

    Returns:
        str: The size, such as "12.3 MB".
    """
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class _ResumeDetailNameEdit(QLineEdit):
    """Show the beginning of a resume detail name when editing ends."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.setCursorPosition(0)

    def select_from_start(self) -> None:
        """Select the entire name with its beginning in view."""
        self.end(False)
        self.home(True)

    def focusOutEvent(self, event: QFocusEvent | None) -> None:
        """Reset horizontal scrolling when leaving the editor.

        Args:
            event (QFocusEvent): The focus event.
        """
        super().focusOutEvent(event)
        if event is not None and event.reason() not in (
            Qt.FocusReason.PopupFocusReason,
            Qt.FocusReason.ActiveWindowFocusReason,
        ):
            self.setCursorPosition(0)
