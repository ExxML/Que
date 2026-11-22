"""Sample real native windows while opening production surfaces with offline fixtures."""

import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ctranslate2  # type: ignore[import-untyped]  # noqa: F401
from PyQt6.QtCore import QMimeData, QPoint, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMenu,
    QMessageBox,
    QToolTip,
    QWidget,
)

from core.ai_sender import AISender
from core.chat_history import ChatHistory
from core.hook_client import HookClient
from core.hook_engine import QUE_KEY, HookEngine
from core.screenshot_manager import ScreenshotManager
from core.settings import SettingsStore
from core.shortcut_manager import ShortcutManager
from core.transcriber import Transcriber
from core.win32_hook import KBDLLHOOKSTRUCT, WM_KEYDOWN, WM_KEYUP
from core.window_policy import install_window_policy, native
from ui.input.input_router import InputRouter
from ui.window.main_window import MainWindow
from ui.window.system_tray import SystemTray


def wait(ms: int) -> None:
    """Process Qt events for at least the given number of milliseconds, like QTest.qWait.

    Args:
        ms (int): The minimum time to keep processing events.
    """
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.002)


def run() -> dict[str, object]:
    """Exercise native visibility transitions without physical input or live services."""
    app = QApplication([])
    if app.platformName() != "windows":
        raise RuntimeError(
            "A Windows Qt platform is required; offscreen is not native coverage."
        )
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    native.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    native.EnumWindows.restype = wintypes.BOOL
    native.IsWindow.argtypes = [wintypes.HWND]
    native.IsWindow.restype = wintypes.BOOL
    native.GetCapture.restype = wintypes.HWND
    native.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    native.CreateWindowExW.restype = wintypes.HWND
    native.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    native.DestroyWindow.argtypes = [wintypes.HWND]
    samples: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    surface = "positive-control"
    stop = threading.Event()

    def inspect(hwnd: int, _parameter: int) -> bool:
        """Check every visible native top-level handle belonging to this process."""
        process = wintypes.DWORD()
        native.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
        if process.value == os.getpid() and native.IsWindowVisible(hwnd):
            affinity = wintypes.DWORD()
            ok = native.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
            if not ok and not native.IsWindow(hwnd):
                return True
            sample = {
                "surface": surface,
                "hwnd": hwnd,
                "affinity": affinity.value,
                "query_ok": bool(ok),
                "time": time.monotonic(),
            }
            samples.append(sample)
            if not ok or affinity.value != 0x11:
                name = ctypes.create_unicode_buffer(256)
                native.GetClassNameW(wintypes.HWND(hwnd), name, 256)
                sample["class"] = name.value
                failures.append(sample)
        return True

    inspect_callback = enum_proc(inspect)

    def sample_loop() -> None:
        """Observe native handles independently of the Qt event loop during opening."""
        while not stop.wait(0.001):
            native.EnumWindows(inspect_callback, 0)

    control = QWidget()
    control.show()
    native.EnumWindows(inspect_callback, 0)
    assert failures, "The sampler must detect the exposed positive control."
    control.hide()
    control.close()
    failures.clear()
    samples.clear()
    policy = install_window_policy(app)
    thread = threading.Thread(target=sample_loop, daemon=True)
    thread.start()
    completed: list[str] = []
    interactions: list[str] = []
    before_focus = None
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SettingsStore(root / "data" / "config.json")
            sender = AISender(settings, client=MagicMock())
            transcriber = Transcriber(settings)
            screenshot = ScreenshotManager()
            screenshot.screenshots_dir = root / "screenshots"
            screenshot.screenshots_dir.mkdir()
            surface = "main-startup"
            with (
                patch("core.provider_catalog.ProviderCatalog.refresh", return_value=1),
                patch(
                    "ui.window.main_window.ChatHistory",
                    return_value=ChatHistory(root / "history.json"),
                ),
                patch.object(HookClient, "start"),
            ):
                window = MainWindow(sender, screenshot, transcriber)
                shortcuts = ShortcutManager(window, screenshot)
                # The helper process's engine, looped back in-process: commands in, events out
                engine = HookEngine(shortcuts.hook_client.dispatch)
                for message in shortcuts.hook_client.state.values():
                    engine.apply(message)
                shortcuts.hook_client.outbox = engine.apply
                router = InputRouter(app, shortcuts)
                tray = SystemTray(window, shortcuts)
            window.visibility_timer.stop()
            wait(80)
            completed.append(surface)
            # Drive the production routing and real Qt widgets, without injecting OS input.
            if window.pages.currentIndex() == 1:
                window.show_settings()
            wait(30)
            edit = window.input_field.input_field
            router.refresh_regions()
            rect = wintypes.RECT()
            hwnd = int(window.winId())
            native.GetWindowRect(hwnd, ctypes.byref(rect))
            center = edit.mapTo(window, edit.rect().center())
            x = rect.left + round(center.x() * window.devicePixelRatioF())
            y = rect.top + round(center.y() * window.devicePixelRatioF())
            before_focus = native.GetForegroundWindow()
            router.on_pointer(0x201, x, y, 0, hwnd)
            router.on_pointer(0x202, x, y, 0, hwnd)
            assert engine.input_armed and router.target is edit, (
                "Clicking input did not arm local editing."
            )

            def key(vk: int, down: bool = True) -> None:
                """Deliver one physical-key fixture to the production callback."""
                event = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=vk)
                result = engine._low_level_keyboard_proc(
                    0, WM_KEYDOWN if down else WM_KEYUP, ctypes.addressof(event)
                )
                assert result == 1, (
                    f"Key {vk} was not consumed; target={router.target}, armed={engine.input_armed}."
                )

            with patch("core.hook_engine.user32") as api:
                api.CallNextHookEx.return_value = 0
                for vk in (ord("H"), ord("E"), ord("L"), ord("L"), ord("O")):
                    key(vk)
                    key(vk, False)
                assert edit.toPlainText() == "hello", edit.toPlainText()
                clipboard = app.clipboard()
                assert clipboard is not None
                saved_clipboard = QMimeData()
                previous = clipboard.mimeData()
                if previous is not None:
                    for content_type in previous.formats():
                        saved_clipboard.setData(
                            content_type, previous.data(content_type)
                        )
                try:
                    key(0x11)
                    key(ord("A"))
                    key(ord("A"), False)
                    key(ord("C"))
                    key(ord("C"), False)
                    key(0x11, False)
                    assert clipboard.text() == "hello"
                    key(0x08)
                    key(0x08, False)
                    key(0x11)
                    key(ord("V"))
                    key(ord("V"), False)
                    key(0x11, False)
                    assert edit.toPlainText() == "hello"
                    interactions.append("copy-paste-select-all")
                finally:
                    clipboard.setMimeData(saved_clipboard)
                key(0x11)
                key(ord("A"))
                key(ord("A"), False)
                key(0x11, False)
                key(ord("X"))
                key(ord("X"), False)
                assert edit.toPlainText() == "x", edit.toPlainText()
                key(0x08)
                key(0x08, False)
                assert edit.toPlainText() == ""
                interactions.append("type-replace-backspace")
            assert native.GetForegroundWindow() == before_focus, (
                "Local editing changed native foreground focus."
            )
            assert not native.GetCapture(), "Local editing grabbed the pointer."
            router.disarm()

            banner = window.status_banner
            height = window.height()
            surface = "warnings-open"
            banner.show_warning("Fixture ongoing warning.")
            banner.show_warning("Fixture one-off warning.", lasting=False)
            wait(40)
            assert banner.isVisible() and banner.rows.count() == 2
            completed.append(surface)
            surface = "warnings-dismissed"
            warning_focus = native.GetForegroundWindow()
            with patch("core.hook_engine.user32") as api:
                key(QUE_KEY)
                key(ord("D"))
                key(ord("D"), False)
                key(QUE_KEY, False)
                api.CallNextHookEx.assert_not_called()
            assert native.GetForegroundWindow() == warning_focus, (
                "Dismissing warnings changed native foreground focus."
            )
            wait(40)
            assert banner.isHidden() and banner.badge.isVisible()
            assert window.height() == height, (
                "Dismissing warnings did not return their space."
            )
            foreground_process = wintypes.DWORD()
            native.GetWindowThreadProcessId(
                native.GetForegroundWindow(), ctypes.byref(foreground_process)
            )
            assert foreground_process.value != os.getpid(), (
                "Que gained foreground focus after dismissing warnings."
            )
            assert not native.GetCapture(), "Dismissing warnings grabbed the pointer."
            completed.append(surface)
            interactions.append("dismiss-warnings-shortcut")
            banner.clear_warning("Fixture ongoing warning.")

            def click(widget: QWidget | None, point: QPoint) -> None:
                """Route a logical widget click through the production mouse receiver."""
                router.refresh_regions()
                assert widget is not None
                top = widget.window()
                assert top is not None
                point_in_top = widget.mapTo(top, point)
                native_point = wintypes.POINT(
                    round(point_in_top.x() * top.devicePixelRatioF()),
                    round(point_in_top.y() * top.devicePixelRatioF()),
                )
                native.ClientToScreen(
                    wintypes.HWND(int(top.winId())), ctypes.byref(native_point)
                )
                router.on_pointer(
                    0x201, native_point.x, native_point.y, 0, int(top.winId())
                )
                router.on_pointer(
                    0x202, native_point.x, native_point.y, 0, int(top.winId())
                )

            def pointer(
                widget: QWidget | None, point: QPoint, message: int, data: int = 0
            ) -> None:
                """Route a motion, wheel, or held-button event at a widget-relative point."""
                router.refresh_regions()
                assert widget is not None
                top = widget.window()
                assert top is not None
                local = widget.mapTo(top, point)
                native_point = wintypes.POINT(
                    round(local.x() * top.devicePixelRatioF()),
                    round(local.y() * top.devicePixelRatioF()),
                )
                native.ClientToScreen(
                    wintypes.HWND(int(top.winId())), ctypes.byref(native_point)
                )
                router.on_pointer(
                    message, native_point.x, native_point.y, data, int(top.winId())
                )

            edit.setPlainText("select this text")
            cursor = edit.textCursor()
            cursor.setPosition(0)
            start_point = edit.cursorRect(cursor).center()
            cursor.setPosition(6)
            end_point = edit.cursorRect(cursor).center()
            pointer(edit.viewport(), start_point, 0x201)
            pointer(edit.viewport(), end_point, 0x200)
            pointer(edit.viewport(), end_point, 0x202)
            assert edit.textCursor().selectedText() == "select", (
                "Routed drag did not select the expected text."
            )
            interactions.append("text-selection-drag")
            original = window.geometry()
            header_point = QPoint(window.width() // 2, 16)
            pointer(window, header_point, 0x201)
            pointer(window, header_point + QPoint(30, 20), 0x200)
            pointer(window, header_point, 0x202)
            assert window.pos() != original.topLeft(), (
                "Header dragging did not move the window."
            )
            assert not native.GetCapture()
            window.setGeometry(original)
            corner = QPoint(window.width() - 2, window.height() - 2)
            pointer(window, corner, 0x201)
            pointer(window, corner + QPoint(40, 30), 0x200)
            pointer(window, corner, 0x202)
            assert (
                window.width() > original.width()
                and window.height() > original.height()
            ), "Edge dragging did not resize the window."
            interactions.append("move-and-resize")
            window.setGeometry(original)
            for index in range(25):
                window.chat_area.add_message(f"Fixture message {index}", False)
            scrollbar = window.chat_area.verticalScrollBar()
            assert scrollbar is not None
            # The range arrives after two layout passes, which take over 80 ms on a 1.5x display
            deadline = time.monotonic() + 5
            while scrollbar.maximum() <= 0 and time.monotonic() < deadline:
                wait(20)
            assert scrollbar.maximum() > 0, (
                "Fixture messages did not overflow the chat."
            )
            scrollbar.setValue(scrollbar.maximum())
            before_scroll = scrollbar.value()
            chat_viewport = window.chat_area.viewport()
            assert chat_viewport is not None
            pointer(chat_viewport, chat_viewport.rect().center(), 0x20A, 120 << 16)
            wait(250)
            assert scrollbar.value() < before_scroll, (
                "Routed wheel did not scroll the chat."
            )
            interactions.append("chat-wheel")
            pointer(window.history_button, window.history_button.rect().center(), 0x200)
            wait(750)
            assert QToolTip.isVisible(), "Routed hover did not show a tooltip."
            interactions.append("hover-tooltip")
            QToolTip.hideText()

            menu = QMenu()
            selected = []
            action = menu.addAction("Fixture action")
            assert action is not None
            action.triggered.connect(lambda: selected.append(True))
            menu.popup(QPoint(100, 100))
            wait(40)
            click(menu, menu.actionGeometry(action).center())
            assert selected == [True], "Routed menu click did not activate its action."
            interactions.append("menu-action")
            menu.close()
            for index in range(3):
                surface = f"main-hide-show-{index}"
                window.hide()
                window.show()
                wait(40)
                completed.append(surface)
            window.show_settings()
            settings_page = window.settings_page
            settings_page.tab_bar.setCurrentIndex(settings_page.tab_bar.count() - 1)
            config = settings_page.config_panel
            config.folder = str(root)
            backup = root / "que-settings.json"
            config_foreground = native.GetForegroundWindow()

            def complete_config_picker(mode: QFileDialog.AcceptMode) -> None:
                """Observe the real protected picker before returning a fixture path."""
                dialog = next(
                    widget
                    for widget in app.topLevelWidgets()
                    if isinstance(widget, QFileDialog) and widget.isVisible()
                )
                assert dialog.acceptMode() == mode
                assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
                wait(80)
                hwnd = int(dialog.winId())
                assert any(
                    row["surface"] == surface and row["hwnd"] == hwnd for row in samples
                )
                dialog.selectFile(str(backup))
                dialog.accept()

            settings_page.instruction_input.setPlainText("Config export fixture")
            surface = "config-export-picker"
            QTimer.singleShot(
                100, lambda: complete_config_picker(QFileDialog.AcceptMode.AcceptSave)
            )
            click(config.export_button, config.export_button.rect().center())
            assert backup.is_file()
            completed.append(surface)
            settings_page.instruction_input.setPlainText("Config import fixture")
            surface = "config-import-picker"
            QTimer.singleShot(
                100, lambda: complete_config_picker(QFileDialog.AcceptMode.AcceptOpen)
            )
            click(config.browse_button, config.browse_button.rect().center())
            completed.append(surface)
            surface = "config-import-review"
            wait(80)
            choice = config.choices["instruction"]
            assert choice.isVisible() and not config.apply_button.isEnabled()
            click(choice, QPoint(8, choice.height() // 2))
            assert choice.isChecked()
            click(config.apply_button, config.apply_button.rect().center())
            assert settings.instruction() == "Config export fixture"
            assert native.GetForegroundWindow() == config_foreground
            completed.append(surface)
            interactions.append("config-transfer")
            audio_tab = settings_page.audio_tab_index
            device_names = {
                "you": ["Fixture microphone"],
                "them": ["Fixture headset [Loopback]"],
            }
            with patch.object(
                transcriber,
                "refresh_audio_devices",
                side_effect=lambda: transcriber.audio_devices_loaded.emit(
                    device_names, ""
                ),
            ) as refresh_devices:
                settings_page.tab_bar.setCurrentIndex(audio_tab)
                wait(20)
                assert settings_page.audio_refresh.isVisible()
                click(
                    settings_page.audio_refresh,
                    settings_page.audio_refresh.rect().center(),
                )
                assert refresh_devices.call_count == 2, (
                    "Routed refresh did not scan for devices."
                )
                interactions.append("audio-refresh")
            for name, combo, tab in (
                ("model", settings_page.model_combo, 0),
                ("effort", settings_page.effort_combo, 0),
                ("transcription", settings_page.whisper_combo, 2),
                ("audio-you", settings_page.audio_combos["you"], audio_tab),
                ("audio-them", settings_page.audio_combos["them"], audio_tab),
            ):
                with patch.object(
                    transcriber,
                    "refresh_audio_devices",
                    side_effect=lambda: transcriber.audio_devices_loaded.emit(
                        device_names, ""
                    ),
                ):
                    settings_page.tab_bar.setCurrentIndex(tab)
                wait(20)
                if tab != audio_tab:
                    combo.setEnabled(True)
                    combo.addItems(["Fixture one", "Fixture two"])
                for repeat in range(3):
                    surface = f"combo-{name}-{repeat}"
                    combo.showPopup()
                    assert not native.GetCapture(), (
                        f"{surface} grabbed the native pointer."
                    )
                    wait(200)
                    opened = combo.view()
                    assert opened is not None
                    opened_window = opened.window()
                    assert opened_window is not None and opened_window.isVisible(), (
                        f"{surface} did not open."
                    )
                    completed.append(surface)
                    combo.hidePopup()
                combo.showPopup()
                wait(200)
                view = combo.view()
                assert view is not None
                model = view.model()
                assert model is not None
                popup_window = view.window()
                assert popup_window is not None
                row = (combo.currentIndex() + 1) % combo.count()
                model_index = model.index(row, 0)
                view.scrollTo(model_index)
                viewport = view.viewport()
                assert viewport is not None
                click(viewport, view.visualRect(model_index).center())
                assert combo.currentIndex() == row, (
                    f"{name} routed selection did not change the value."
                )
                assert not popup_window.isVisible(), (
                    f"{name} did not close after selection."
                )
                interactions.append(f"combo-{name}-selection")
                combo.setCurrentIndex(0)
                assert combo.isVisible(), f"{name} fixture is not on the visible tab."
                router._focus(combo)
                combo.showPopup()
                wait(200)
                router.refresh_regions()
                with patch("core.hook_engine.user32") as api:
                    api.CallNextHookEx.return_value = 0
                    key(0x28)
                    key(0x28, False)
                    key(0x0D)
                    key(0x0D, False)
                assert combo.currentIndex() == 1, (
                    f"{name} keyboard selection did not change the value."
                )
                assert not popup_window.isVisible(), (
                    f"{name} keyboard selection did not dismiss the popup."
                )
                interactions.append(f"combo-{name}-keyboard")
                if tab == audio_tab:
                    source = name.removeprefix("audio-")
                    assert settings.audio_device(source) == device_names[source][0], (
                        f"{name} selection was not persisted."
                    )
            for name, field in (
                ("input", window.input_field.input_field),
                ("settings", window.settings_page.key_input),
            ):
                surface = f"context-{name}"
                context_menu = field.createStandardContextMenu()
                assert context_menu is not None
                menu = context_menu
                menu.popup(QPoint(100, 100))
                wait(80)
                assert menu.isVisible()
                assert not native.GetCapture()
                completed.append(surface)
                menu.close()
                menu.deleteLater()
            surface = "tray-menu"
            tray.menu.popup(QPoint(200, 200))
            wait(80)
            assert tray.menu.isVisible()
            assert not native.GetCapture()
            completed.append(surface)
            tray.menu.close()
            surface = "tooltip"
            QToolTip.showText(QPoint(100, 100), "Fixture tooltip", window)
            wait(150)
            assert QToolTip.isVisible()
            completed.append(surface)
            QToolTip.hideText()
            for mode in (
                QFileDialog.FileMode.Directory,
                QFileDialog.FileMode.ExistingFile,
            ):
                surface = f"dialog-{mode.name}"
                dialog = QFileDialog(window, "Fixture picker", str(root))
                dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
                dialog.setFileMode(mode)
                dialog.show()
                wait(100)
                assert dialog.isVisible()
                completed.append(surface)
                dialog.close()
                dialog.deleteLater()
            surface = "message-dialog"
            dialog_box = QMessageBox(window)
            dialog_box.setText("Fixture message")
            dialog_box.show()
            wait(80)
            completed.append(surface)
            dialog_box.close()
            surface = "handle-recreation"
            replacement = QWidget()
            replacement.show()
            wait(40)
            replacement.setWindowFlag(Qt.WindowType.Tool)
            replacement.show()
            wait(40)
            completed.append(surface)
            replacement.close()
            surface = "failed-affinity-show"
            blocked = QWidget()
            with patch.object(native, "SetWindowDisplayAffinity", return_value=0):
                blocked.show()
                wait(40)
                assert not native.IsWindowVisible(int(blocked.winId())), (
                    "A failed protection call exposed a window."
                )
            blocked.hide()
            blocked.show()
            wait(40)
            assert native.IsWindowVisible(int(blocked.winId())), (
                "Protected retry must recover."
            )
            blocked.close()
            completed.append(surface)
            surface = "native-window-guard"
            raw = native.CreateWindowExW(
                0,
                "STATIC",
                "Fixture native surface",
                0x80000000,
                20,
                20,
                100,
                100,
                None,
                None,
                None,
                None,
            )
            assert raw, "Could not create a native control window."
            try:
                with patch.object(native, "SetWindowDisplayAffinity", return_value=0):
                    native.ShowWindow(raw, 4)
                    assert not native.IsWindowVisible(raw), (
                        "Native protection failure did not block visibility."
                    )
                native.ShowWindow(raw, 4)
                wait(50)
                assert native.IsWindowVisible(raw), (
                    "Native protection retry did not recover."
                )
                affinity = wintypes.DWORD()
                assert (
                    native.GetWindowDisplayAffinity(raw, ctypes.byref(affinity))
                    and affinity.value == 0x11
                )
            finally:
                native.DestroyWindow(raw)
            completed.append(surface)
            router.close()
            tray.hide()
            for widget in app.topLevelWidgets():
                widget.close()
            assert all(
                any(row["surface"] == name for row in samples) for name in completed
            ), "Every surface must have native observations."
    finally:
        stop.set()
        thread.join(2)
        policy.close()
    report = {
        "scope": "Synthetic opening and routed callback input; no browser pixels or physical input.",
        "completed": completed,
        "interactions": interactions,
        "foreground_hwnd": before_focus,
        "foreground_check": "unavailable"
        if not before_focus
        else "unchanged during routed editing",
        "samples": len(samples),
        "failures": failures,
        "policy_errors": policy.errors,
    }
    report_path = Path(".stealth-runs/repair-native-surfaces-report.json")
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2))
    assert not failures, json.dumps(
        {
            "failures": list(
                {
                    (str(row["surface"]), str(row.get("class", "")), row["hwnd"])
                    for row in failures
                }
            )
        }
    )
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
