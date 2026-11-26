import sys

from core.hook_host import run_hook_host

# The input helper is this same program; start it before loading the heavy UI and AI libraries
if __name__ == "__main__" and len(sys.argv) in (3, 4) and sys.argv[1] == "--hook-host":
    sys.exit(run_hook_host(*sys.argv[2:]))

import onnxruntime  # type: ignore[import-untyped]  # noqa: F401  # Load before PyQt6, whose older msvcp140.dll crashes onnxruntime
from PyQt6.QtWidgets import QApplication

from core.ai_sender import AISender
from core.screenshot_manager import ScreenshotManager
from core.shortcut_manager import ShortcutManager
from core.transcriber import Transcriber
from core.window_policy import install_window_policy
from ui.input.input_router import InputRouter
from ui.window.main_window import MainWindow
from ui.window.system_tray import SystemTray

if __name__ == "__main__":
    # Launch the application and its components
    app = QApplication(sys.argv)
    window_policy = install_window_policy(app)
    screenshot_manager = ScreenshotManager()
    ai_sender = AISender(live=True)
    transcriber = Transcriber(ai_sender.settings_store)
    main_window = MainWindow(ai_sender, screenshot_manager, transcriber)
    app.aboutToQuit.connect(transcriber.stop)
    app.aboutToQuit.connect(main_window.speaker.stop)
    app.aboutToQuit.connect(main_window.worker.stop)
    app.aboutToQuit.connect(ai_sender.reset_chat)
    app.aboutToQuit.connect(ai_sender.claude_bridge.close)
    shortcut_manager = ShortcutManager(main_window, screenshot_manager)
    input_router = InputRouter(app, shortcut_manager)
    app.aboutToQuit.connect(shortcut_manager.stop)
    tray_icon = SystemTray(main_window, shortcut_manager)
    tray_icon.setVisible(ai_sender.settings_store.system_tray())
    main_window.settings_page.system_tray_saved.connect(tray_icon.setVisible)

    sys.exit(app.exec())


# nuitka-project: --windows-icon-from-ico={MAIN_DIRECTORY}/assets/que.ico
# nuitka-project: --windows-console-mode=disable
# nuitka-project: --enable-plugin=pyqt6
# nuitka-project: --include-package=google.genai
# nuitka-project: --include-package=onnx_asr
# nuitka-project: --include-package-data=onnx_asr
# Speech loads its voice lazily; espeak-ng's library and data ship inside espeakng_loader.
# nuitka-project: --include-package=kokoro_onnx
# nuitka-project: --include-package-data=kokoro_onnx
# nuitka-project: --include-package=phonemizer
# nuitka-project: --include-package-data=espeakng_loader
# Hugging Face loads download helpers lazily. Its lock helpers also load SoftFileLock on Windows.
# nuitka-project: --include-package=huggingface_hub.utils
# nuitka-project: --include-module=filelock._soft
# nuitka-project: --include-data-dir={MAIN_DIRECTORY}/assets=assets
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/assets/que_hook.dll=assets/que_hook.dll

# The generated Gemini schema produces a C file large enough to exhaust MSVC's optimizer.
# nuitka-project: --noinclude-custom-mode=google.genai.types:bytecode
# Ship the runtime SDK without its tests or Pydantic's mypy plugin.
# nuitka-project: --nofollow-import-to=google.genai.tests,pydantic.mypy
