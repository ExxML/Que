"""Inspect instantiated UI in a separate, offline, staged source process."""

import json
import socket
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch


def inspect_controls(source: Path, output: Path) -> None:
    """Instantiate Qt controls with synthetic state and denied network access."""
    sys.path.insert(0, str(source))
    import ctranslate2  # type: ignore[import-untyped]  # noqa: F401
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication, QWidget

    from core.ai_sender import AISender
    from core.chat_history import Conversation
    from core.model_catalog import ModelOption
    from core.screenshot_manager import ScreenshotManager
    from core.settings import SettingsStore
    from core.shortcut_manager import ShortcutManager
    from core.transcriber import Transcriber, TranscriptSegment
    from ui.window.main_window import MainWindow
    from ui.window.system_tray import SystemTray

    app = QApplication([])
    with (
        patch.object(
            socket.socket, "connect", side_effect=RuntimeError("Offline inventory")
        ),
        patch("core.provider_catalog.ProviderCatalog.refresh", return_value=1),
        patch("core.hook_client.HookClient.start"),
        patch.object(MainWindow, "show"),
    ):
        settings = SettingsStore(source / "data" / "config.json")
        sender = AISender(settings)
        screenshot = ScreenshotManager()
        transcriber = Transcriber(settings)
        window = MainWindow(sender, screenshot, transcriber)
        shortcut = ShortcutManager(window, screenshot)
        tray = SystemTray(window, shortcut)
        options = (ModelOption("audit-model", "Synthetic model", ("default",)),)
        sender.set_models(options)
        window.input_field.input_settings.set_models(options)
        window.chat_area.add_message(
            "Synthetic audit chat", True, "Synthetic transcript"
        )
        window.chat_area.add_message("Synthetic audit response", False)
        window.history_sidebar.set_conversations(
            [Conversation("gemini", "Synthetic audit chat", conversation_id="audit")],
            "audit",
        )
        image_path = source / "data" / "cache" / "screenshots" / "audit.png"
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(0xFF00FF)
        image.save(str(image_path))
        screenshot.screenshot_added.emit(str(image_path))
        window.input_field.input_settings.model_dropdown._show_popup()
        transcriber.segments = [
            TranscriptSegment(1, "you", "Synthetic microphone phrase."),
            TranscriptSegment(2, "them", "Synthetic loopback phrase."),
        ]
        transcriber.changed.emit()
        backup = source / "config-fixture.json"
        backup.write_text(
            json.dumps(
                {
                    "format": "que-settings",
                    "version": 1,
                    "settings": {"instruction": "Synthetic backup instruction"},
                }
            ),
            encoding="utf-8",
        )
        with patch("ui.window.config_panel.choose_file", return_value=str(backup)):
            window.settings_page.config_panel.browse_button.click()
        rows: list[dict[str, Any]] = []
        for widget in app.allWidgets():
            cls = type(widget).__name__
            if (
                cls == "QCheckBox" and widget.accessibleName().startswith("Import ")
            ) or any(
                token in cls.lower()
                for token in (
                    "button",
                    "combo",
                    "edit",
                    "tabbar",
                    "list",
                    "menu",
                    "dropdown",
                )
            ):
                parent = widget.parent()
                rows.append(
                    {
                        "class": cls,
                        "name": widget.objectName(),
                        "accessible": widget.accessibleName(),
                        "parent_class": type(parent).__name__ if parent else "",
                        "enabled": widget.isEnabled(),
                    }
                )
        output.write_text(
            json.dumps(
                sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)), indent=2
            ),
            encoding="utf-8",
        )
        window.visibility_timer.stop()
        window.input_field.input_settings.close_popups()
        window.close()
        tray.hide()
        for widget in app.topLevelWidgets():
            if isinstance(widget, QWidget):
                widget.close()


if __name__ == "__main__":
    inspect_controls(Path(sys.argv[1]), Path(sys.argv[2]))
