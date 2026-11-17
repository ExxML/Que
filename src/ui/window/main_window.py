import ctypes
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.ai_receiver import AIReceiver
from core.ai_sender import AISender, InteractionRequest
from core.chat_history import ChatHistory, Conversation
from core.provider_catalog import ProviderCatalog, ProviderResult
from core.screenshot_manager import ScreenshotManager
from core.transcriber import Transcriber
from core.window_policy import install_window_policy
from ui.chat.chat_area import ChatArea
from ui.directory_picker import choose_directory
from ui.input.input_field import InputField
from ui.theme import BG_COLOR, DANGER_COLOR, PRIMARY_COLOR, WINDOW_ALPHA, qcolor, qss
from ui.window.header_icon_button import HeaderIconButton
from ui.window.history_sidebar import HistorySidebar
from ui.window.screenshot_tray import ScreenshotTray
from ui.window.screenshot_viewer import ScreenshotViewer
from ui.window.settings_page import SettingsPage
from ui.window.transcript_panel import TranscriptPanel


class MainWindow(QWidget):
    """Render the main application window and its chat controls."""

    BG_COLOR = qcolor(BG_COLOR, WINDOW_ALPHA)
    quit_requested = pyqtSignal()
    mouse_routing_changed = pyqtSignal(bool)

    def __init__(
        self,
        ai_sender: AISender,
        screenshot_manager: ScreenshotManager,
        transcriber: Transcriber | None = None,
    ) -> None:
        super().__init__()
        application = QApplication.instance()
        assert isinstance(application, QApplication)
        self.window_policy = install_window_policy(application)
        self.ai_sender = ai_sender
        self.screenshot_manager = screenshot_manager
        self.transcriber = transcriber or Transcriber(ai_sender.settings_store)
        self.chat_history = ChatHistory()
        # The open chat keeps one saved conversation per provider, mirroring AISender.contexts
        self.conversations: dict[str, Conversation | None] = {
            "gemini": None,
            "claude": None,
        }
        self.capture_hidden = False
        self.topmost_error = ""  # Last reported visibility-check failure
        self._init_UI()
        self.worker = AIReceiver(ai_sender, self.chat_area)
        self.window_policy.failed.connect(self.on_window_policy_failed)
        for error in self.window_policy.errors:
            self.chat_area.show_stream_error(error)
        self.worker.turn_committed.connect(self._on_turn_committed)
        self.input_field.model_changed.connect(self.ai_sender.set_model)
        self.input_field.effort_changed.connect(self.ai_sender.set_effort_level)
        self.model_catalog = ProviderCatalog(self.ai_sender.claude_bridge)
        self.catalog_revision = 0
        self.model_catalog.completed.connect(self._on_models_loaded)
        self.model_catalog.failed.connect(self._on_models_failed)
        self.input_field.input_settings.reload_requested.connect(self._refresh_models)
        self.settings_page.saved.connect(self._on_settings_saved)
        self.settings_page.directory_saved.connect(self._set_claude_directory)
        self.settings_page.capture_hidden_saved.connect(self.set_capture_hidden)
        self.settings_page.click_through_saved.connect(self.set_click_through)
        self.input_field.input_settings.directory_requested.connect(
            self._choose_claude_directory
        )
        self.input_field.input_settings.directory_cleared.connect(
            lambda: self._set_claude_directory("")
        )
        self.input_field.input_settings.record_button.clicked.connect(
            self.transcriber.toggle
        )
        self.transcriber.recording_changed.connect(
            self.input_field.input_settings.set_recording
        )
        self._show_claude_directory()
        self._refresh_models()
        if self.ai_sender.provider == "gemini" and not self.ai_sender.api_key:
            self.settings_page.open_settings()
            self.pages.setCurrentIndex(1)
            self.input_field.hide()
            self.settings_button.setText("Back to chat")

    def toggle_window_visibility(self) -> None:
        """Toggle the main window between visible and hidden states."""
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()  # Bring to front

    def on_window_policy_failed(self, message: str) -> None:
        """Defer error rendering until the native window callback has returned.

        Args:
            message (str): The window protection failure.
        """
        QTimer.singleShot(0, lambda: self.chat_area.show_stream_error(message))

    def hide(self) -> None:
        """Close settings popups and flush the backing store before hiding."""
        if hasattr(self, "input_field"):
            self.input_field.input_settings.close_popups()
            self.settings_page.model_combo.hidePopup()
            self.settings_page.effort_combo.hidePopup()
            self.settings_page.key_input.setEchoMode(
                self.settings_page.key_input.EchoMode.Password
            )
            self.settings_page.show_key.setChecked(False)
            if hasattr(self, "screenshot_viewer"):
                self.screenshot_viewer.close_viewer()
            self.repaint()
            QApplication.processEvents()
        super().hide()

    def set_capture_hidden(self, hidden: bool) -> None:
        """Exclude Que's windows from or include them in screen capture (Windows 10+).

        Args:
            hidden (bool): Whether screenshots, recordings, and screen sharing should omit Que.
        """
        self.window_policy.set_capture_hidden(hidden)
        # A failed exclusion leaves the window capturable, so the red border must show then too
        self.capture_hidden = hidden and self.window_policy.protect(int(self.winId()))
        self.update()

    def set_click_through(self, enabled: bool) -> None:
        """Let mouse input pass through the window to the windows behind it.

        Args:
            enabled (bool): Whether the window ignores mouse input.
        """
        self.mouse_routing_changed.emit(not enabled)

    def send_message(self, message: str) -> None:
        """Send a user message with any pending screenshot attachments.

        Args:
            message (str): The user's message text.
        """
        if not self.ai_sender.model:
            self.chat_area.show_stream_error(
                "Open Settings to connect your AI provider, or retry loading models."
            )
            return
        # Sent screenshots move into history storage so the chat can show them later
        attachments = self.chat_history.keep_screenshots(
            self.screenshot_manager.get_and_clear_pending()
        )
        self.screenshot_tray.clear()
        # The transcript stays pending until the turn commits, so failed turns resend it
        transcript, transcript_seq = self.transcriber.pending()
        self.worker.handle_message(
            message, attachments or None, transcript, transcript_seq
        )

    def toggle_history(self) -> None:
        """Open or close the conversation history sidebar."""
        if not self.history_sidebar.opened:
            self.input_field.input_settings.close_popups()
            self.screenshot_viewer.close_viewer()
            self._refresh_history()
            self._position_history_sidebar()
        self.history_sidebar.toggle()

    def show_settings(self) -> None:
        """Open provider settings in the same capture-excluded overlay."""
        self.history_sidebar.close_sidebar()
        self.screenshot_viewer.close_viewer()
        if self.pages.currentIndex() == 1:
            self._close_settings()
            return
        self.input_field.input_settings.close_popups()
        self.settings_page.open_settings()
        self.pages.setCurrentIndex(1)
        self.input_field.hide()
        self.screenshot_tray.hide()
        self.transcript_panel.hide()
        self.settings_button.setText("Back to chat")

    def quit_app(self) -> None:
        """Request shutdown after the input hooks finish consumed physical presses."""
        self.quit_requested.emit()

    def finish_quit(self) -> None:
        """Quit the application, stopping any active worker and clearing chat."""
        # Stop any active worker
        if self.worker is not None:
            self.worker.stop()
        self.transcriber.stop()

        self.chat_area.clear_chat_messages()
        self.ai_sender.reset_chat()
        self.screenshot_manager.clear_screenshots()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Reposition the floating input overlays when the window is resized.

        Args:
            event (QResizeEvent): The resize event passed to the base widget.
        """
        super().resizeEvent(event)
        self._position_screenshot_tray()
        self._position_history_sidebar()
        self._position_screenshot_viewer()

    # Override mousePressEvent to automatically set focus to input field
    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        """Set focus to the input field when the window is clicked.

        Args:
            event (QMouseEvent): The mouse press event.
        """
        if self.pages.currentIndex() == 0:
            self.input_field.input_field.setFocus()
        super().mousePressEvent(event)

    # Override paintEvent to draw app window
    def paintEvent(self, _event: QPaintEvent | None) -> None:
        """Paint the main window with rounded corners and a border."""
        painter = QPainter(self)
        self._paint_frame(painter)

    def _init_UI(self) -> None:
        """Initialize the main window UI layout and components."""
        # Config variables
        self.window_width = 550
        self.window_height = 600

        # Set window flags for main window behavior
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)

        # Translucent bg for rounded corners
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Prevent cursor from changing when hovering over the window
        self.setAttribute(Qt.WidgetAttribute.WA_SetCursor, False)
        self.unsetCursor()

        # Window setup (position main window at center-top on screen)
        self.primary_screen = QApplication.primaryScreen()
        assert self.primary_screen is not None
        screen_rect = self.primary_screen.availableGeometry()
        self.setGeometry(
            (screen_rect.width() - self.window_width) // 2,
            2,
            self.window_width,
            self.window_height,
        )

        # Create main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Create chat area
        self.chat_area = ChatArea(self)

        # Create input field
        self.input_field = InputField(self)

        self.pages = QStackedWidget(self)
        # Keep page scrollbars inside the 1px window border instead of flush with the window edge
        self.pages.setContentsMargins(0, 0, 4, 0)
        self.pages.addWidget(self.chat_area)
        self.settings_page = SettingsPage(
            self.ai_sender.settings_store,
            self.ai_sender.claude_bridge,
            self.transcriber,
            self,
        )
        self.pages.addWidget(self.settings_page)

        # Create screenshot tray as a floating overlay (not in any layout)
        self.screenshot_tray = ScreenshotTray(self.screenshot_manager, self)

        # Transcript panel is docked above the input; the window grows by its height
        self.transcript_panel = TranscriptPanel(self.transcriber, self)
        self.transcript_panel_height = 0  # Height currently added to the window

        # Create and add title bar buttons
        header_button_style = f"""
            QPushButton {{
                border: none;
            }}
            QPushButton:hover {{
                background-color: {qss(PRIMARY_COLOR, 128)};
                border-radius: 0px;
                margin-top: 1px;
            }}
        """
        self.history_button = HeaderIconButton("menu", self)
        self.history_button.setFixedSize(36, 32)
        self.history_button.setAccessibleName("Conversation history")
        self.history_button.setToolTip("History (Right Ctrl+H)")
        self.history_button.clicked.connect(self.toggle_history)
        self.history_button.setStyleSheet(
            header_button_style
            + "QPushButton:hover { border-top-left-radius: 7px; margin-left: 1px; }"
        )
        self.new_chat_button = HeaderIconButton("plus", self)
        self.new_chat_button.setFixedSize(36, 32)
        self.new_chat_button.setAccessibleName("New chat")
        self.new_chat_button.setToolTip("New chat (Right Ctrl+N)")
        self.new_chat_button.clicked.connect(self._new_chat)
        self.new_chat_button.setStyleSheet(header_button_style)
        header_layout = QHBoxLayout()
        header_layout.addWidget(self.history_button)
        header_layout.addWidget(self.new_chat_button)
        header_layout.addStretch(1)
        self.settings_button = QPushButton("Settings", self)
        self.settings_button.setAccessibleName("AI settings")
        self.settings_button.setStyleSheet(
            f"QPushButton {{ color: {qss(PRIMARY_COLOR, 230)}; background: transparent; border: none; padding: 6px; }}"
        )
        self.settings_button.clicked.connect(self.show_settings)
        header_layout.addWidget(self.settings_button)
        self.min_btn = QPushButton("–", self)
        self.min_btn.setFixedSize(36, 32)
        self.min_btn.clicked.connect(self.hide)
        self.min_btn.setStyleSheet(f"""
            QPushButton {{
                color: {qss(PRIMARY_COLOR, 200)};
                border: none;
                font-size: 24px;
                font-weight: bold;
                padding-bottom: 3px;
                text-align: center;
            }}
            QPushButton:hover {{
                background-color: {qss(PRIMARY_COLOR, 128)};
                color: rgba(0, 0, 0, 153);
                border-radius: 0px;
                margin-top: 1px;
            }}
        """)
        self.close_btn = QPushButton("×", self)
        self.close_btn.setFixedSize(36, 32)
        self.close_btn.clicked.connect(self.quit_app)
        self.close_btn.setStyleSheet(f"""
            QPushButton {{
            color: {qss(PRIMARY_COLOR, 200)};
                border: none;
                font-size: 24px;
                font-weight: bold;
                padding-bottom: 3px;
                text-align: center;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 77, 69, 128);
                color: rgba(0, 0, 0, 153);
                border-radius: 0px;
                border-top-right-radius: 7px;
                margin-top: 1px;
                margin-right: 1px;
            }}
        """)
        header_layout.addWidget(self.min_btn)
        header_layout.addWidget(self.close_btn)
        main_layout.addLayout(header_layout)

        # Add chat area
        main_layout.addWidget(self.pages, stretch=1)
        main_layout.addWidget(self.transcript_panel)

        # Add input field
        self.input_field.message_sent.connect(self.send_message)
        self.input_field.height_changed.connect(self._on_input_height_changed)
        main_layout.addWidget(self.input_field)

        # Position the floating screenshot tray above the input field and transcript panel
        self.screenshot_tray.raise_()
        self.screenshot_tray.visibility_changed.connect(self._position_screenshot_tray)
        self.transcript_panel.size_changed.connect(self._sync_transcript_panel)

        # Create the history sidebar as a floating overlay over everything below the header
        self.history_sidebar = HistorySidebar(self, self._paint_frame)
        self.history_sidebar.conversation_selected.connect(self._open_conversation)
        self.history_sidebar.conversation_deleted.connect(self._delete_conversation)
        self.history_sidebar.closed.connect(self._on_history_closed)

        # Sent screenshots open enlarged over everything below the header
        self.screenshot_viewer = ScreenshotViewer(self, self._paint_frame)
        self.chat_area.screenshot_opened.connect(self._open_screenshots)

        # Unset cursor for all child widgets to preserve system cursor
        self._unset_cursor_recursive(self)

        self.set_capture_hidden(self.ai_sender.settings_store.capture_hidden())
        self.show()

        # Setup timer to raise main window so it is always visible (certain Windows operations override the stay on top hint)
        self.visibility_timer = QTimer(self)
        self.visibility_timer.setInterval(1000)
        self.visibility_timer.timeout.connect(self._ensure_window_visible)
        self.visibility_timer.start()

    def _on_models_loaded(self, revision: int, result: ProviderResult) -> None:
        """Install discovered models on the Qt thread.

        Args:
            revision (int): The catalog request identifier.
            result (ProviderResult): The provider's discovered models.
        """
        if (
            revision != self.catalog_revision
            or result.provider != self.ai_sender.provider
        ):
            return
        self._apply_models(result, use_defaults=not bool(self.ai_sender.model))

    def _on_models_failed(self, revision: int, error: str) -> None:
        """Display only the latest provider discovery error.

        Args:
            revision (int): The catalog request identifier.
            error (str): The discovery error.
        """
        if revision == self.catalog_revision:
            self.input_field.input_settings.show_model_error(error)

    def _refresh_models(self) -> None:
        """Reload only the active provider's available models."""
        self.input_field.input_settings.show_loading()
        self.catalog_revision = self.model_catalog.refresh(
            self.ai_sender.provider, self.ai_sender.api_key
        )

    def _apply_models(self, result: ProviderResult, use_defaults: bool = True) -> None:
        """Synchronize the sender and selectors with valid saved defaults.

        Args:
            result (ProviderResult): The active provider's model catalog.
            use_defaults (bool): Whether to restore saved defaults instead of current chat overrides.
        """
        self.ai_sender.set_models(result.models)
        model, effort = self.ai_sender.settings_store.defaults(result.provider)
        if use_defaults and model in self.ai_sender.models:
            self.ai_sender.set_model(model)
        if (
            use_defaults
            and effort in self.ai_sender.models[self.ai_sender.model].effort_levels
        ):
            self.ai_sender.set_effort_level(effort)
        self.input_field.input_settings.set_models(
            result.models, self.ai_sender.model, self.ai_sender.effort_level
        )
        self.input_field.input_settings.model_dropdown.setToolTip(
            f"{result.provider.title()} · Model for this chat"
        )

    def _on_settings_saved(self, result: ProviderResult) -> None:
        """Apply automatic updates while keeping settings open and current turns intact.

        Args:
            result (ProviderResult): The verified provider configuration.
        """
        self.catalog_revision = -1
        api_key = self.ai_sender.settings_store.gemini_key()
        if (
            result.provider != self.ai_sender.provider
            or api_key != self.ai_sender.api_key
        ):
            self.worker.stop()
            self.chat_area.finalize_assistant_stream()
            self.ai_sender.configure(result.provider, api_key)
            self._show_claude_directory()
        self._apply_models(result)

    def _choose_claude_directory(self) -> None:
        """Move Claude to another directory for subsequent turns of this session."""
        self.input_field.input_settings.close_popups()
        directory = choose_directory(
            self, str(self.ai_sender.claude_directory or Path.home())
        )
        if directory:
            self._set_claude_directory(directory)

    def _set_claude_directory(self, directory: str) -> None:
        """Use a directory for subsequent Claude turns.

        Args:
            directory (str): The directory Claude works in, or an empty string for no directory.
        """
        self.ai_sender.claude_directory = Path(directory) if directory else None
        self._show_claude_directory()

    def _show_claude_directory(self) -> None:
        """Show Claude's directory below the input while Claude is the active provider."""
        directory = self.ai_sender.claude_directory
        input_settings = self.input_field.input_settings
        input_settings.set_directory(str(directory) if directory else "")
        input_settings.directory_widget.setVisible(self.ai_sender.provider == "claude")

    def _close_settings(self) -> None:
        """Return to the current chat while automatic validation continues."""
        self.pages.setCurrentIndex(0)
        self.settings_page.key_input.setEchoMode(
            self.settings_page.key_input.EchoMode.Password
        )
        self.settings_page.show_key.setChecked(False)
        self.input_field.show()
        self.settings_button.setText("Settings")
        self.screenshot_tray.setVisible(bool(self.screenshot_manager.pending_paths))
        self._sync_transcript_panel()
        self.input_field.input_field.setFocus()

    def _new_chat(self) -> None:
        """Start a new chat in the default directory with the history sidebar closed."""
        self.history_sidebar.close_sidebar()
        self._set_claude_directory(self.ai_sender.settings_store.claude_directory())
        self._clear_all_chat()

    def _clear_all_chat(self) -> None:
        """Clear all chat data, screenshots, and screenshot tray thumbnails."""
        self.worker.stop()
        self.screenshot_viewer.close_viewer()
        self.chat_area.clear_chat_messages()
        self.ai_sender.reset_chat()
        self.screenshot_manager.clear_screenshots()
        self.screenshot_tray.clear()
        self.transcriber.clear()
        self.conversations = {"gemini": None, "claude": None}
        self._refresh_history()
        if self.ai_sender.models:
            self._apply_models(
                ProviderResult(
                    self.ai_sender.provider, tuple(self.ai_sender.models.values()), ""
                )
            )

    def _position_screenshot_tray(self) -> None:
        """Place the screenshot tray above the input field and any transcript panel."""
        tray = self.screenshot_tray
        if self.pages.currentIndex() == 1:
            tray.hide()
            return
        layout = self.layout()
        assert layout is not None
        layout.activate()  # Apply pending panel visibility changes before reading positions
        panel = self.transcript_panel
        tray_h = tray.height()
        tray.setGeometry(
            0,
            (panel.y() if panel.isVisible() else self.input_field.y()) - tray_h,
            self.width(),
            tray_h,
        )

    def _sync_transcript_panel(self) -> None:
        """Show the transcript panel on the chat page and grow the window by its height.

        Like the input field, the panel extends the window downward instead of
        shrinking or covering the chat.
        """
        panel = self.transcript_panel
        height = panel.height() if panel.wanted() else 0
        delta = height - self.transcript_panel_height
        self.transcript_panel_height = height
        panel.setVisible(panel.wanted() and self.pages.currentIndex() == 0)
        if delta:
            self.resize(self.width(), self.height() + delta)
        self._position_screenshot_tray()

    def _paint_frame(self, painter: QPainter) -> None:
        """Paint the rounded window background and border in window coordinates.

        Args:
            painter (QPainter): The painter to draw with.
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        radius = 8

        # Draw window with rounded corners
        painter.setBrush(self.BG_COLOR)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, radius, radius)

        # Draw window border
        border_width = 1
        border_rect = rect.adjusted(
            border_width, border_width, -border_width, -border_width
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        border_color = (
            qcolor(PRIMARY_COLOR, 128) if self.capture_hidden else qcolor(DANGER_COLOR)
        )
        painter.setPen(QPen(border_color, border_width))
        painter.drawRoundedRect(border_rect, radius, radius)

    def _position_history_sidebar(self) -> None:
        """Place the sidebar below the header along the left window edge."""
        top = self.pages.y()
        self.history_sidebar.setGeometry(
            0, top, HistorySidebar.PANEL_WIDTH, self.height() - top
        )

    def _position_screenshot_viewer(self) -> None:
        """Place the screenshot viewer over everything below the header."""
        top = self.pages.y()
        self.screenshot_viewer.setGeometry(0, top, self.width(), self.height() - top)

    def _open_screenshots(self, paths: list[str], index: int) -> None:
        """Show a sent message's screenshots enlarged.

        Args:
            paths (list[str]): The message's screenshot files.
            index (int): The screenshot to show first.
        """
        self.history_sidebar.close_sidebar()
        self.input_field.input_settings.close_popups()
        self._position_screenshot_viewer()
        self.screenshot_viewer.open_screenshots(paths, index)

    def _refresh_history(self) -> None:
        """Rebuild the sidebar list and highlight the open conversation."""
        active = self.conversations[self.ai_sender.provider]
        self.history_sidebar.set_conversations(
            self.chat_history.conversations(),
            active.conversation_id if active is not None else "",
        )

    def _on_history_closed(self) -> None:
        """Return keyboard focus to the message input after closing the sidebar."""
        if self.pages.currentIndex() == 0:
            self.input_field.input_field.setFocus()

    def _on_turn_committed(
        self,
        interaction_request: InteractionRequest,
        interaction_id: str,
        response: str,
    ) -> None:
        """Save a completed turn to the conversation of its provider.

        Args:
            interaction_request (InteractionRequest): The completed request snapshot.
            interaction_id (str): The accepted server interaction identifier.
            response (str): The full assistant response text.
        """
        provider = interaction_request.provider
        conversation = self.conversations[provider]
        if conversation is None:
            title = " ".join(interaction_request.message.split())[:80]
            conversation = Conversation(provider, title or "Untitled chat")
            self.conversations[provider] = conversation
        self.transcriber.mark_sent(interaction_request.transcript_seq)
        user_message: dict[str, Any] = {
            "role": "user",
            "text": interaction_request.message,
        }
        if interaction_request.transcript:
            user_message["transcript"] = interaction_request.transcript
        screenshots = self.chat_history.screenshot_names(
            interaction_request.attachments
        )
        if screenshots:
            user_message["screenshots"] = screenshots
        conversation.messages.append(user_message)
        conversation.messages.append({"role": "assistant", "text": response})
        conversation.interaction_id = interaction_id
        conversation.session_id = (
            interaction_request.session_id if provider == "claude" else ""
        )
        conversation.updated = time.time()
        try:
            self.chat_history.save(conversation)
        except OSError as e:
            print(f"Error saving chat history: {e!s}")
        if self.history_sidebar.opened:
            self._refresh_history()

    def _open_conversation(self, conversation_id: str) -> None:
        """Show a saved conversation and resume its provider context.

        Args:
            conversation_id (str): The saved conversation identifier.
        """
        self.history_sidebar.close_sidebar()
        self.screenshot_viewer.close_viewer()
        if self.pages.currentIndex() == 1:
            self._close_settings()
        conversation = self.chat_history.get(conversation_id)
        if conversation is None or conversation in self.conversations.values():
            return
        self.worker.stop()
        if conversation.provider != self.ai_sender.provider:
            # Switch only for this session; the saved default provider stays unchanged
            self.ai_sender.configure(conversation.provider, self.ai_sender.api_key)
            self._show_claude_directory()
            self._refresh_models()
        self.ai_sender.restore_chat(
            conversation.interaction_id, conversation.session_id
        )
        self.conversations = {"gemini": None, "claude": None}
        self.conversations[conversation.provider] = conversation
        self.chat_area.load_messages(
            [
                (
                    message["text"],
                    message["role"] == "user",
                    message.get("transcript", ""),
                    self.chat_history.screenshot_paths(message),
                )
                for message in conversation.messages
            ]
        )

    def _delete_conversation(self, conversation_id: str) -> None:
        """Delete a saved conversation, starting a new chat if it is open.

        Args:
            conversation_id (str): The saved conversation identifier.
        """
        try:
            self.chat_history.delete(conversation_id)
        except OSError as e:
            print(f"Error saving chat history: {e!s}")
        if any(
            conversation is not None and conversation.conversation_id == conversation_id
            for conversation in self.conversations.values()
        ):
            self._clear_all_chat()
        self._refresh_history()

    # Set cursor as default texture regardless of where it is hovering on the main window
    def _unset_cursor_recursive(self, widget: QWidget) -> None:
        """Recursively unset cursor for a widget and all its children.

        Args:
            widget (QWidget): The widget to unset the cursor for.
        """
        widget.setAttribute(Qt.WidgetAttribute.WA_SetCursor, False)
        widget.unsetCursor()
        for child in widget.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_SetCursor, False)
            child.unsetCursor()

    def _on_input_height_changed(self, delta_height: int) -> None:
        """Resize the window downward when the input field height changes.

        Args:
            delta_height (int): The change in height of the input field in pixels.
        """
        self.resize(self.width(), self.height() + delta_height)

    def _ensure_window_visible(self) -> None:
        """Raise the main window if a window above it covers it."""
        if self.isVisible() and self._is_topmost_window() is False:
            self.raise_()

    def _is_topmost_window(self) -> bool | None:
        """Check that no window above the main window covers its corner positions.

        Returns:
            bool | None: True if the main window is topmost at all sampled points, False if a
                window above covers one, or None when the check failed (reported, never assumed).
        """
        try:
            rect = self.frameGeometry()
            assert self.primary_screen is not None
            scale = self.primary_screen.devicePixelRatio()
            ga_root = 2
            self_hwnd = int(self.winId())
            self_root = ctypes.windll.user32.GetAncestor(
                wintypes.HWND(self_hwnd), ga_root
            )
            # Compare z-order instead of identity: WindowFromPoint skips click-through windows
            # (Que itself and transparent overlays), so it returns the window beneath, which
            # must not count as covering this one
            gw_hwndprev = 3
            windows_above: set[int] = set()
            hwnd = self_root
            while hwnd := ctypes.windll.user32.GetWindow(hwnd, gw_hwndprev):
                windows_above.add(int(hwnd))
            padding = 15
            corners = [
                rect.topLeft() + QPoint(padding, padding),
                rect.topRight() + QPoint(-padding, padding),
                rect.bottomLeft() + QPoint(padding, -padding),
                rect.bottomRight() + QPoint(-padding, -padding),
            ]
            for corner in corners:
                pt = wintypes.POINT(int(corner.x() * scale), int(corner.y() * scale))
                hwnd_at_pt = ctypes.windll.user32.WindowFromPoint(pt)
                if hwnd_at_pt:
                    target_root = ctypes.windll.user32.GetAncestor(
                        wintypes.HWND(hwnd_at_pt), ga_root
                    )
                    if int(target_root) in windows_above:
                        return False
            self.topmost_error = ""
            return True
        except Exception as error:  # noqa: BLE001
            message = f"Could not verify Que's window visibility: {error}"
            if self.topmost_error != message:
                self.topmost_error = message
                self.chat_area.show_stream_error(message)
            return None
