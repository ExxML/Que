"""Protect Qt and native transient windows before their first visible frame."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PyQt6 import sip
from PyQt6.QtCore import (
    QEvent,
    QObject,
    Qt,
    pyqtSignal,
)
from PyQt6.QtWidgets import QApplication, QWidget

from core.win32_hook import HOOKPROC

WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
WS_CHILD = 0x40000000
SWP_SHOWWINDOW = 0x40
SWP_HIDEWINDOW = 0x80

native = ctypes.WinDLL("user32", use_last_error=True)
native.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
native.SetWindowDisplayAffinity.restype = wintypes.BOOL
native.GetWindowDisplayAffinity.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
native.GetWindowDisplayAffinity.restype = wintypes.BOOL
native.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
native.GetWindowLongPtrW.restype = ctypes.c_ssize_t
native.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
native.SetWindowLongPtrW.restype = ctypes.c_ssize_t
native.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    HOOKPROC,
    wintypes.HINSTANCE,
    wintypes.DWORD,
]
native.SetWindowsHookExW.restype = wintypes.HHOOK
native.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
native.UnhookWindowsHookEx.restype = wintypes.BOOL
native.CallNextHookEx.argtypes = [
    wintypes.HHOOK,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
native.CallNextHookEx.restype = wintypes.LPARAM
native.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
native.GetAncestor.restype = wintypes.HWND
native.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
native.GetWindowThreadProcessId.restype = wintypes.DWORD
native.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
native.GetWindowRect.restype = wintypes.BOOL
native.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
native.GetWindow.restype = wintypes.HWND
native.IsWindowVisible.argtypes = [wintypes.HWND]
native.IsWindowVisible.restype = wintypes.BOOL
native.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_ubyte,
    wintypes.DWORD,
]
native.SetLayeredWindowAttributes.restype = wintypes.BOOL
SUBCLASSPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    ctypes.c_size_t,
    ctypes.c_size_t,
)
controls = ctypes.WinDLL("comctl32", use_last_error=True)
controls.SetWindowSubclass.argtypes = [
    wintypes.HWND,
    SUBCLASSPROC,
    ctypes.c_size_t,
    ctypes.c_size_t,
]
controls.SetWindowSubclass.restype = wintypes.BOOL
controls.RemoveWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t]
controls.RemoveWindowSubclass.restype = wintypes.BOOL
controls.DefSubclassProc.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
controls.DefSubclassProc.restype = wintypes.LPARAM


class WindowPolicy(QObject):
    """Enforce capture exclusion and non-activation for all application surfaces."""

    failed = pyqtSignal(str)

    def __init__(self, application: QApplication) -> None:
        super().__init__(application)
        self.application = application
        self.errors: list[str] = []
        self.capture_hidden = True
        self.preparing: set[int] = set()
        self.subclassed: set[int] = set()
        self.subclass_proc = SUBCLASSPROC(self._window_proc)
        self.call_hook: int | None = None
        self.cbt_hook: int | None = None
        self.call_proc = HOOKPROC(self._on_native_message)
        self.cbt_proc = HOOKPROC(self._on_activation)
        self.native_enabled = application.platformName() == "windows"
        for effect in (
            Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateMenu,
            Qt.UIEffect.UI_FadeMenu,
            Qt.UIEffect.UI_AnimateTooltip,
            Qt.UIEffect.UI_FadeTooltip,
            Qt.UIEffect.UI_AnimateToolBox,
        ):
            application.setEffectEnabled(effect, False)
        if self.native_enabled:
            thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            self.call_hook = native.SetWindowsHookExW(
                4, self.call_proc, None, thread_id
            )
            if not self.call_hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self.cbt_hook = native.SetWindowsHookExW(5, self.cbt_proc, None, thread_id)
            if not self.cbt_hook:
                error = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(error)
        application.installEventFilter(self)
        application.aboutToQuit.connect(self.close)

    def close(self) -> None:
        """Remove the GUI thread hooks after the application finishes."""
        self.native_enabled = False
        self.application.removeEventFilter(self)
        for hwnd in tuple(self.subclassed):
            controls.RemoveWindowSubclass(hwnd, self.subclass_proc, 1)
        self.subclassed.clear()
        for handle in (self.call_hook, self.cbt_hook):
            if handle:
                native.UnhookWindowsHookEx(handle)
        self.call_hook = self.cbt_hook = None

    def set_capture_hidden(self, hidden: bool) -> None:
        """Choose whether Que's windows are excluded from capture and apply it to visible windows.

        Args:
            hidden (bool): Whether screenshots, recordings and screen sharing omit Que.
        """
        self.capture_hidden = hidden
        for widget in self.application.topLevelWidgets():
            if widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created):
                self.protect(int(widget.winId()))

    def protect(self, hwnd: int) -> bool:
        """Verify the chosen capture affinity and native input styles before showing a window.

        Args:
            hwnd (int): The application's native top-level window handle.

        Returns:
            bool: Whether all requested native protections were verified.
        """
        if not self.native_enabled:
            return False
        if hwnd in self.preparing:
            return True
        self.preparing.add(hwnd)
        try:
            if hwnd not in self.subclassed:
                if not controls.SetWindowSubclass(hwnd, self.subclass_proc, 1, 0):
                    raise OSError("Could not install the window visibility guard.")
                self.subclassed.add(hwnd)
            style = int(native.GetWindowLongPtrW(hwnd, -20))
            required = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
            if style & required != required:
                ctypes.set_last_error(0)
                result = native.SetWindowLongPtrW(hwnd, -20, style | required)
                if not result and ctypes.get_last_error():
                    raise ctypes.WinError(ctypes.get_last_error())
                widget = QWidget.find(sip.voidptr(hwnd))
                if (
                    not style & WS_EX_LAYERED
                    and (
                        widget is None
                        or not widget.testAttribute(
                            Qt.WidgetAttribute.WA_TranslucentBackground
                        )
                    )
                    and not native.SetLayeredWindowAttributes(hwnd, 0, 255, 2)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            if int(native.GetWindowLongPtrW(hwnd, -20)) & required != required:
                raise OSError("Window input protection could not be verified.")
            wanted = WDA_EXCLUDEFROMCAPTURE if self.capture_hidden else WDA_NONE
            if not native.SetWindowDisplayAffinity(hwnd, wanted):
                raise ctypes.WinError(ctypes.get_last_error())
            affinity = wintypes.DWORD()
            if not native.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
                raise ctypes.WinError(ctypes.get_last_error())
            if affinity.value != wanted:
                raise OSError("Window capture affinity could not be verified.")
            return True
        except Exception as error:  # noqa: BLE001  # Block visibility if protection cannot be established.
            self._report(f"Window protection failed for {hwnd}: {error}")
            return False
        finally:
            self.preparing.discard(hwnd)

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """Prepare every Qt top-level widget, including internally created popups."""
        if isinstance(watched, QWidget) and watched.isWindow() and event is not None:
            if event.type() in (QEvent.Type.Polish, QEvent.Type.Show):
                if watched.windowType() == Qt.WindowType.Popup:
                    watched.setProperty("quePopup", True)
                    watched.setWindowFlags(
                        Qt.WindowType.Tool
                        | Qt.WindowType.FramelessWindowHint
                        | Qt.WindowType.WindowStaysOnTopHint
                        | Qt.WindowType.WindowDoesNotAcceptFocus
                        | Qt.WindowType.WindowTransparentForInput
                    )
                elif event.type() == QEvent.Type.Polish:
                    watched.setWindowFlags(
                        watched.windowFlags()
                        | Qt.WindowType.WindowDoesNotAcceptFocus
                        | Qt.WindowType.WindowTransparentForInput
                    )
                watched.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                watched.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
            if event.type() in (QEvent.Type.Show, QEvent.Type.WinIdChange):
                hwnd = (
                    int(watched.winId())
                    if event.type() == QEvent.Type.Show
                    else (
                        int(watched.winId())
                        if watched.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
                        else 0
                    )
                )
                if hwnd:
                    protected = self.protect(hwnd)
                    if self.native_enabled and event.type() == QEvent.Type.Show:
                        watched.setAttribute(
                            Qt.WidgetAttribute.WA_DontShowOnScreen, not protected
                        )
        return False

    def _report(self, message: str) -> None:
        """Retain and emit each protection error without creating another window.

        Args:
            message (str): The protection failure to report.
        """
        if message not in self.errors:
            self.errors.append(message)
            self.failed.emit(message)

    def _on_activation(self, code: int, wparam: int, lparam: int) -> int:
        """Veto native activation and focus transfers into the application's GUI thread."""
        try:
            if code in (5, 9):  # HCBT_ACTIVATE and HCBT_SETFOCUS.
                return 1
            return int(native.CallNextHookEx(self.cbt_hook, code, wparam, lparam))
        except BaseException:  # noqa: BLE001  # Last-resort native callback boundary.
            return 1 if code in (5, 9) else 0

    def _on_native_message(self, code: int, wparam: int, lparam: int) -> int:
        """Install a visibility guard before new native top-level windows can be shown."""
        try:
            if code >= 0:
                message = ctypes.cast(
                    lparam, ctypes.POINTER(_CallWindowMessage)
                ).contents
                if (
                    message.message in (0x1, 0x46)
                    and not native.GetWindowLongPtrW(message.hwnd, -16) & WS_CHILD
                ):
                    hwnd = int(message.hwnd)
                    if hwnd not in self.subclassed and controls.SetWindowSubclass(
                        hwnd, self.subclass_proc, 1, 0
                    ):
                        self.subclassed.add(hwnd)
                    if message.message == 0x46:
                        position = ctypes.cast(
                            message.lparam, ctypes.POINTER(_WindowPosition)
                        ).contents
                        if position.flags & SWP_SHOWWINDOW:
                            self.protect(hwnd)
            return int(native.CallNextHookEx(self.call_hook, code, wparam, lparam))
        except BaseException:  # noqa: BLE001  # Last-resort native callback boundary.
            return 0

    def _window_proc(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
        _subclass_id: int,
        _reference: int,
    ) -> int:
        """Enforce visibility in the actual window procedure, where position changes are writable."""
        position: _WindowPosition | None = None
        try:
            if message == 0x46:
                position = ctypes.cast(lparam, ctypes.POINTER(_WindowPosition)).contents
                position.flags |= 0x10
                if position.flags & SWP_SHOWWINDOW and not self.protect(hwnd):
                    position.flags = (position.flags & ~SWP_SHOWWINDOW) | SWP_HIDEWINDOW
            if message == 0x82:  # WM_NCDESTROY; HWND values may subsequently be reused.
                controls.RemoveWindowSubclass(hwnd, self.subclass_proc, 1)
                self.subclassed.discard(hwnd)
            return int(controls.DefSubclassProc(hwnd, message, wparam, lparam))
        except BaseException:  # noqa: BLE001  # Native callbacks must not unwind into Windows.
            if position is not None:
                position.flags = (position.flags & ~SWP_SHOWWINDOW) | SWP_HIDEWINDOW
            return 0


def install_window_policy(application: QApplication) -> WindowPolicy:
    """Install one retained policy before creating any application windows.

    Args:
        application (QApplication): The application whose windows need protection.

    Returns:
        WindowPolicy: The application-owned policy instance.
    """
    existing = application.findChild(WindowPolicy)
    return existing if existing is not None else WindowPolicy(application)


class _CallWindowMessage(ctypes.Structure):
    """Represent a WH_CALLWNDPROC message with pointer-sized fields."""

    _fields_ = [
        ("lparam", wintypes.LPARAM),
        ("wparam", wintypes.WPARAM),
        ("message", wintypes.UINT),
        ("hwnd", wintypes.HWND),
    ]


class _WindowPosition(ctypes.Structure):
    """Represent a native window's proposed position and visibility flags."""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("after", wintypes.HWND),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("cy", ctypes.c_int),
        ("flags", wintypes.UINT),
    ]
