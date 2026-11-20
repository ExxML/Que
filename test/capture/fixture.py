"""Drive real Que surfaces in a disposable source process, exclusively on the Qt thread."""

from __future__ import annotations

import argparse
import ctypes
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# The runner passes its staged source explicitly; never fall back to the user's data directory.


def run(source: Path) -> int:
    """Create the production widget tree and service bounded fixture commands."""
    sys.path.insert(0, str(source))
    import ctranslate2  # type: ignore[import-untyped]  # noqa: F401
    import win32api  # type: ignore[import-untyped]
    from PyQt6.QtCore import QPoint, Qt, QTimer
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMenu, QToolTip, QWidget

    from core.ai_sender import AISender
    from core.hook_client import HookClient
    from core.screenshot_manager import ScreenshotManager
    from core.settings import SettingsStore
    from core.shortcut_manager import ShortcutManager
    from core.transcriber import Transcriber, TranscriptSegment
    from core.window_policy import install_window_policy
    from ui.input.input_router import InputRouter
    from ui.window.main_window import MainWindow
    from ui.window.system_tray import SystemTray

    application = QApplication([])
    if application.platformName() != "windows":
        raise RuntimeError("Capture fixtures require the real Windows Qt backend.")
    policy = install_window_policy(application)
    settings = SettingsStore(source / "data" / "config.json")
    sender = AISender(settings, client=MagicMock())
    transcriber = Transcriber(settings)
    screenshot = ScreenshotManager()
    commands: queue.Queue[dict[str, Any]] = queue.Queue()
    window: MainWindow | None = None
    tray: SystemTray | None = None
    router: InputRouter | None = None
    retained: list[QWidget] = []
    stream_timer = QTimer()
    sequence = 0
    opened: list[str] = []
    open_source = transcriber._open_source

    def record_open(audio: Any, source: str) -> Any:
        """Note each WASAPI stream that actually opened; healthy sources emit no status."""
        capture = open_source(audio, source)
        opened.append(source)
        return capture

    patch.object(transcriber, "_open_source", side_effect=record_open).start()

    def read() -> None:
        """Marshal input commands without touching Qt objects from the pipe thread."""
        try:
            for line in sys.stdin:
                commands.put(json.loads(line))
        finally:
            commands.put({"command": "quit"})

    def reply(value: dict[str, Any]) -> None:
        """Write a distinct protocol line despite third-party diagnostic output."""
        print(f"CAPTURE:{json.dumps(value)}", flush=True)

    def clean() -> None:
        """Dismiss prior transient surfaces while preserving normal production timers."""
        stream_timer.stop()
        transcriber.stop()
        transcriber.segments.clear()
        transcriber.changed.emit()
        QToolTip.hideText()
        for widget in retained:
            widget.close()
            if tray is None or widget is not tray.menu:
                widget.deleteLater()
        retained.clear()
        if window is not None:
            window.hide()
            window.history_sidebar.close_sidebar()  # Resets its open state for the next toggle.
            window.history_sidebar.hide()
            window.screenshot_viewer.close_viewer()
            window.pages.setCurrentIndex(0)
            window.input_field.show()
            window.settings_page.model_combo.hidePopup()
            window.settings_page.effort_combo.hidePopup()
            window.screenshot_tray.hide()

    def chunk() -> None:
        """Render deterministic response chunks through the production chat widget."""
        if window is not None:
            window.chat_area.append_to_stream("Synthetic streaming response. ")

    def dispatch() -> None:
        """Apply one action and acknowledge the real resulting native/widget state."""
        nonlocal window, tray, router, sequence
        if commands.empty():
            return
        command = commands.get_nowait()
        name = str(command.get("command", ""))
        try:
            if name == "quit":
                clean()
                if tray is not None:
                    tray.hide()
                if router is not None:
                    router.close()
                application.quit()
                return
            if name == "hide":
                clean()
            elif name == "state":
                pass
            elif name == "audio":
                if command.get("enabled"):
                    model = MagicMock()
                    model.transcribe.return_value = ([], None)
                    transcriber.model = model
                    transcriber.model_size = "fixture"
                    # Exercise the actual microphone/WASAPI capture while keeping model
                    # inference and downloads outside this capture-regression fixture.
                    opened.clear()
                    with patch.object(
                        transcriber, "active_model", return_value="fixture"
                    ):
                        transcriber.start()
                else:
                    transcriber.stop()
            elif name == "capture":
                policy.set_capture_hidden(bool(command["hidden"]))
            elif name == "surface":
                clean()
                surface = str(command["surface"])
                if window is None:
                    # Only remote providers and the physical-input helper are replaced.
                    # Native window protection, Qt painting, and the raising timer remain real.
                    with (
                        patch(
                            "core.provider_catalog.ProviderCatalog.refresh",
                            return_value=1,
                        ),
                        patch.object(HookClient, "start"),
                    ):
                        window = MainWindow(sender, screenshot, transcriber)
                        shortcut = ShortcutManager(window, screenshot)
                        tray = SystemTray(window, shortcut)
                        tray.setVisible(settings.system_tray())
                        router = InputRouter(application, shortcut)
                    window.chat_area.add_message("QUE CAPTURE FIXTURE", is_user=True)
                # The runner numbers displays in Win32 order; Qt keeps each native origin.
                left, top = win32api.GetMonitorInfo(
                    win32api.EnumDisplayMonitors()[int(command.get("monitor", 0))][0]
                )["Monitor"][:2]
                screen = next(
                    item
                    for item in application.screens()
                    if (item.geometry().x(), item.geometry().y()) == (left, top)
                )
                rect = screen.geometry()
                window.move(
                    rect.x() + rect.width() // 3,
                    rect.y() + max(110, rect.height() // 6),
                )
                window.resize(550, 600)
                window.show()
                expected: QWidget = window
                if surface == "shutdown":
                    window.close()
                elif surface == "hide-show":
                    window.hide()
                    window.show()
                elif surface == "move-resize":
                    window.move(window.x() + 40, window.y() + 10)
                    window.resize(600, 620)
                elif surface == "click-through":
                    window.set_click_through(True)
                    window.set_click_through(False)
                elif surface == "settings":
                    window.show_settings()
                elif surface == "history":
                    window.toggle_history()
                    expected = window.history_sidebar
                elif surface in ("screenshot-tray", "screenshot-viewer"):
                    path = source / "data" / "fixture.png"
                    image = QImage(160, 100, QImage.Format.Format_RGB32)
                    image.fill(Qt.GlobalColor.cyan)
                    image.save(str(path))
                    if surface == "screenshot-tray":
                        screenshot.screenshot_added.emit(str(path))
                        expected = window.screenshot_tray
                    else:
                        window.screenshot_viewer.open_screenshots([str(path)], 0)
                        expected = window.screenshot_viewer
                elif surface == "transcript":
                    transcriber.segments = [
                        TranscriptSegment(1, "you", "Synthetic transcript.")
                    ]
                    transcriber.changed.emit()
                    expected = window.transcript_panel
                elif surface in ("model-popup", "effort-popup"):
                    window.show_settings()
                    combo = (
                        window.settings_page.model_combo
                        if surface == "model-popup"
                        else window.settings_page.effort_combo
                    )
                    combo.setEnabled(True)
                    combo.addItems(["Fixture one", "Fixture two"])
                    combo.showPopup()
                    view = combo.view()
                    assert view is not None
                    popup = view.window()
                    assert popup is not None
                    expected = popup
                elif surface in ("custom-model-popup", "custom-effort-popup"):
                    dropdown = (
                        window.input_field.input_settings.model_dropdown
                        if surface == "custom-model-popup"
                        else window.input_field.input_settings.effort_dropdown
                    )
                    dropdown.blockSignals(True)
                    dropdown.set_options(
                        [("Fixture one", "one"), ("Fixture two", "two")]
                    )
                    dropdown.blockSignals(False)
                    dropdown.setEnabled(True)
                    dropdown.show()
                    dropdown._show_popup()
                    assert dropdown.popup is not None
                    expected = dropdown.popup
                elif surface == "tooltip":
                    QToolTip.showText(
                        window.mapToGlobal(QPoint(60, 90)),
                        "Fixture tooltip",
                        window,
                        window.rect(),
                        60000,
                    )
                elif surface in ("context-menu", "tray-menu"):
                    menu = (
                        window.input_field.input_field.createStandardContextMenu()
                        if surface == "context-menu"
                        else tray.menu
                        if tray is not None
                        else QMenu(window)
                    )
                    assert menu is not None
                    menu.popup(window.mapToGlobal(QPoint(80, 100)))
                    retained.append(menu)
                    expected = menu
                elif surface in ("file-dialog", "directory-dialog"):
                    dialog = QFileDialog(window, "Fixture picker", str(source / "data"))
                    dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
                    dialog.setFileMode(
                        QFileDialog.FileMode.Directory
                        if surface == "directory-dialog"
                        else QFileDialog.FileMode.ExistingFile
                    )
                    dialog.move(window.pos())
                    dialog.show()
                    retained.append(dialog)
                    expected = dialog
                elif surface == "provider-error":
                    window.chat_area.show_stream_error("Synthetic provider error.")
                elif surface == "recording-error":
                    transcriber.failed.emit("Synthetic recording error.", True)
                elif surface == "streaming":
                    window.chat_area.start_assistant_stream()
                    stream_timer.start(100)
                elif surface == "handle-recreation":
                    widget = QWidget(window, Qt.WindowType.Tool)
                    widget.setGeometry(window.geometry())
                    widget.show()
                    widget.setWindowFlag(Qt.WindowType.FramelessWindowHint)
                    widget.show()
                    retained.append(widget)
                    expected = widget
                elif surface not in ("startup", "main"):
                    raise ValueError(f"Unknown surface {surface}.")
                application.processEvents()
                if surface != "shutdown" and not expected.isVisible():
                    raise RuntimeError(f"Requested {surface} did not become visible.")
                if surface == "tooltip" and not QToolTip.isVisible():
                    raise RuntimeError("Tooltip did not become visible.")
            else:
                raise ValueError(f"Unknown fixture command {name}.")
            application.processEvents()
            sequence += 1
            visible = [
                widget for widget in application.topLevelWidgets() if widget.isVisible()
            ]
            reply(
                {
                    "id": command.get("id"),
                    "ok": True,
                    "sequence": sequence,
                    "command": name,
                    "surface": command.get("surface"),
                    "visible": len(visible),
                    "windows": [
                        {
                            "hwnd": int(widget.winId()),
                            "class": type(widget).__name__,
                            "rect": [
                                widget.x(),
                                widget.y(),
                                widget.width(),
                                widget.height(),
                            ],
                        }
                        for widget in visible
                    ],
                    "errors": policy.errors,
                    "foreground": int(ctypes.windll.user32.GetForegroundWindow()),
                    "recording": transcriber.recording,
                    "source_health": dict(transcriber.source_health),
                    "audio_sources": list(opened),
                }
            )
        except Exception as error:  # noqa: BLE001  # Report failed fixture commands without unwinding through Qt.
            reply(
                {
                    "id": command.get("id"),
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    stream_timer.timeout.connect(chunk)
    timer = QTimer()
    timer.timeout.connect(dispatch)
    timer.start(20)
    threading.Thread(target=read, daemon=True).start()
    reply(
        {
            "ready": True,
            "displays": [
                {
                    "name": screen.name(),
                    "geometry": [
                        screen.geometry().x(),
                        screen.geometry().y(),
                        screen.geometry().width(),
                        screen.geometry().height(),
                    ],
                    "dpi": screen.logicalDotsPerInch(),
                    "ratio": screen.devicePixelRatio(),
                }
                for screen in application.screens()
            ],
        }
    )
    application.setQuitOnLastWindowClosed(False)
    return int(application.exec())


def main() -> int:
    """Require a staged source tree with a disposable data directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    if not (source / "data" / ".que-audit-fixture").is_file():
        parser.error("Refuse to run without the staging marker.")
    return run(source)


if __name__ == "__main__":
    sys.exit(main())
