import ctypes
import time
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
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
    QScrollArea,
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
from ui.chat.chat_area import SCROLLBAR_STYLE, ChatArea
from ui.directory_picker import choose_directory
from ui.input.input_field import InputField
from ui.theme import BG_COLOR, PRIMARY_COLOR, WINDOW_ALPHA, qcolor, qss
from ui.window.header_icon_button import HeaderIconButton
from ui.window.history_sidebar import HistorySidebar
from ui.window.screenshot_tray import ScreenshotTray
from ui.window.settings_page import SettingsPage
from ui.window.transcript_panel import TranscriptPanel


class MainWindow(QWidget):
    """Render the main application window and its chat controls."""

    BG_COLOR = qcolor(BG_COLOR, WINDOW_ALPHA)

    def __init__(
        self,
        ai_sender: AISender,
        screenshot_manager: ScreenshotManager,
        transcriber: Transcriber | None = None,
    ) -> None:
        super().__init__()
        self.ai_sender = ai_sender
        self.screenshot_manager = screenshot_manager
        self.transcriber = transcriber or Transcriber(ai_sender.settings_store)
        self.chat_history = ChatHistory()
        # The open chat keeps one saved conversation per provider, mirroring AISender.contexts
        self.conversations: dict[str, Conversation | None] = {
            "gemini": None,
            "claude": None,
        }
        self._init_UI()
        self.worker = AIReceiver(ai_sender, self.chat_area)
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
            self.repaint()
            QApplication.processEvents()
        super().hide()

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
        attachments = self.screenshot_manager.get_and_clear_pending()
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
            self._refresh_history()
            self._position_history_sidebar()
        self.history_sidebar.toggle()

    def show_settings(self) -> None:
        """Open provider settings in the same capture-excluded overlay."""
        self.history_sidebar.close_sidebar()
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
            # Qt.WindowType.WindowTransparentForInput # Click-through
        )

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
        self.pages.addWidget(self.chat_area)
        self.settings_page = SettingsPage(
            self.ai_sender.settings_store,
            self.ai_sender.claude_bridge,
            self.transcriber,
            self,
        )
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        settings_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        # Styled on the bar itself so combo popups inside settings keep their own look
        settings_scrollbar = settings_scroll.verticalScrollBar()
        assert settings_scrollbar is not None
        settings_scrollbar.setStyleSheet(SCROLLBAR_STYLE)
        settings_scroll.setWidget(self.settings_page)
        self.pages.addWidget(settings_scroll)

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
        self.history_button.setToolTip("History (Ctrl+H)")
        self.history_button.clicked.connect(self.toggle_history)
        self.history_button.setStyleSheet(
            header_button_style
            + "QPushButton:hover { border-top-left-radius: 7px; margin-left: 1px; }"
        )
        self.new_chat_button = HeaderIconButton("plus", self)
        self.new_chat_button.setFixedSize(36, 32)
        self.new_chat_button.setAccessibleName("New chat")
        self.new_chat_button.setToolTip("New chat (Ctrl+N)")
        self.new_chat_button.clicked.connect(self._clear_all_chat)
        self.new_chat_button.setStyleSheet(header_button_style)
        header_layout = QHBoxLayout()
        header_layout.addWidget(self.history_button)
        header_layout.addWidget(self.new_chat_button)
        header_layout.addStretch(1)
        self.settings_button = QPushButton("Settings", self)
        self.settings_button.setAccessibleName("AI settings")
        self.settings_button.setStyleSheet(
            f"QPushButton {{ color: {qss(PRIMARY_COLOR, 180)}; background: transparent; border: none; padding: 6px; }}"
        )
        self.settings_button.clicked.connect(self.show_settings)
        header_layout.addWidget(self.settings_button)
        self.min_btn = QPushButton("–", self)
        self.min_btn.setFixedSize(36, 32)
        self.min_btn.clicked.connect(self.hide)
        self.min_btn.setStyleSheet(f"""
            QPushButton {{
                color: {qss(PRIMARY_COLOR, 77)};
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
            color: {qss(PRIMARY_COLOR, 77)};
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

        # Unset cursor for all child widgets to preserve system cursor
        self._unset_cursor_recursive(self)

        self.show()

        # Set display affinity to exclude main window from screen capture (Windows 10+)
        hwnd = int(self.winId())
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        result = ctypes.windll.user32.SetWindowDisplayAffinity(
            hwnd, WDA_EXCLUDEFROMCAPTURE
        )
        if result == 0:
            print(
                "Warning: SetWindowDisplayAffinity failed. May appear in screenshots."
            )

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

    def _clear_all_chat(self) -> None:
        """Clear all chat data, screenshots, and screenshot tray thumbnails."""
        self.worker.stop()
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
        painter.setPen(QPen(qcolor(PRIMARY_COLOR, 128), border_width))
        painter.drawRoundedRect(border_rect, radius, radius)

    def _position_history_sidebar(self) -> None:
        """Place the sidebar below the header along the left window edge."""
        top = self.pages.y()
        self.history_sidebar.setGeometry(
            0, top, HistorySidebar.PANEL_WIDTH, self.height() - top
        )

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
        user_message = {"role": "user", "text": interaction_request.message}
        if interaction_request.transcript:
            user_message["transcript"] = interaction_request.transcript
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
        """Raise the main window if it is no longer the topmost window."""
        try:
            if not self._is_topmost_window():
                self.raise_()
        except Exception:  # noqa: BLE001, S110
            pass

    def _is_topmost_window(self) -> bool:
        """Check if the main window is the topmost window at its corner positions.

        Returns:
            bool: True if the main window is topmost at all sampled points.
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
                    if int(self_root) != int(target_root):
                        return False
            return True
        except Exception:  # noqa: BLE001
            return True
