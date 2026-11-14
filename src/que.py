import sys

import ctranslate2  # type: ignore[import-untyped]  # noqa: F401  # Load before PyQt6, whose DLLs crash CTranslate2 model loading otherwise
from PyQt6.QtWidgets import QApplication

from core.ai_sender import AISender
from core.screenshot_manager import ScreenshotManager
from core.shortcut_manager import ShortcutManager
from core.transcriber import Transcriber
from ui.window.main_window import MainWindow
from ui.window.system_tray import SystemTray

if __name__ == "__main__":
    # Launch the application and its components
    app = QApplication(sys.argv)
    screenshot_manager = ScreenshotManager()
    ai_sender = AISender()
    transcriber = Transcriber(ai_sender.settings_store)
    main_window = MainWindow(ai_sender, screenshot_manager, transcriber)
    app.aboutToQuit.connect(transcriber.stop)
    app.aboutToQuit.connect(main_window.worker.stop)
    app.aboutToQuit.connect(ai_sender.reset_chat)
    app.aboutToQuit.connect(ai_sender.claude_bridge.close)
    shortcut_manager = ShortcutManager(main_window, screenshot_manager)
    tray_icon = SystemTray(main_window, shortcut_manager)
    tray_icon.setVisible(ai_sender.settings_store.system_tray())
    main_window.settings_page.system_tray_saved.connect(tray_icon.setVisible)

    sys.exit(app.exec())
