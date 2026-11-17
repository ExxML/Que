"""Run Que's low-level input hooks in a helper process that Que's own work can never stall.

Windows silently drops a low-level hook whose callback misses its deadline and lets
the event through. Hosting the hooks in a separate process keeps them independent of
Que's interpreter lock and UI work, and a Raw Input listener in the same process
detects any physical event the hooks missed so they can be restored at once.
"""

import ctypes
import ctypes.wintypes
import gc
import json
import os
import queue
import sys
import threading
from multiprocessing.connection import Client, Connection
from typing import Any

from core.hook_engine import HookEngine, InputMonitor, key_identity
from core.win32_hook import HOOKPROC, WH_KEYBOARD_LL, WH_MOUSE_LL, kernel32, user32

# Private handles so these signatures never change the shared ctypes.windll functions
native = ctypes.WinDLL("user32", use_last_error=True)
system = ctypes.WinDLL("kernel32", use_last_error=True)

AUTH_ENVIRONMENT = "QUE_HOOK_AUTH"
WM_INPUT = 0x00FF
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
HWND_MESSAGE = -3
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RI_KEY_BREAK = 0x01
RI_KEY_E0 = 0x02
RI_KEY_E1 = 0x04
TIMER_ID = 1
TIMER_MS = 100
HEARTBEAT_TICKS = 10
# Raw mouse button flags -> (hook button value, pressed)
RAW_BUTTONS = {
    0x0001: (1, True),
    0x0002: (1, False),
    0x0004: (2, True),
    0x0008: (2, False),
    0x0010: (4, True),
    0x0020: (4, False),
    0x0040: (8, True),
    0x0080: (8, False),
    0x0100: (16, True),
    0x0200: (16, False),
}
RAW_WHEELS = {0x0400: 0x20A, 0x0800: 0x20E}
BUTTON_NAMES = {1: "left", 2: "right", 4: "middle", 8: "back", 16: "forward"}

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.wintypes.LPARAM,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


def run_hook_host(address: str) -> int:
    """Connect to Que, host the hooks until Que disconnects and owned input drains.

    Args:
        address (str): The named pipe Que listens on.

    Returns:
        int: The process exit code.
    """
    authkey = bytes.fromhex(os.environ.pop(AUTH_ENVIRONMENT, ""))
    try:
        commands = Client(address, family="AF_PIPE", authkey=authkey)
        events = Client(address, family="AF_PIPE", authkey=authkey)
    except OSError:
        return 2
    host = HookHost(commands, events)
    return host.run()


class HookHost:
    """Own the hooks, the Raw Input leak detector, and the pipe to Que on one message-loop thread."""

    def __init__(self, commands: Connection, events: Connection) -> None:
        self.commands = commands
        self.events = events
        self.outbox: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
        self.engine = HookEngine(self.outbox.put)
        self.engine.monitor = InputMonitor()
        self.keyboard_proc = HOOKPROC(self.engine._low_level_keyboard_proc)
        self.mouse_proc = HOOKPROC(self.engine._mouse_proc)
        self.window_proc = WNDPROC(self._window_proc)
        self.thread_id = 0
        self.window = 0
        self.connected = True
        self.ticks = 0

    def run(self) -> int:
        """Install the hooks and pump messages until shutdown.

        Returns:
            int: Zero after a clean shutdown, otherwise one.
        """
        self.thread_id = int(system.GetCurrentThreadId())
        system.SetPriorityClass(system.GetCurrentProcess(), 0x00008000)
        system.SetThreadPriority(system.GetCurrentThread(), 2)
        switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.0005)  # Hand the interpreter to the hook thread quickly
        # A collection pass would stall the hook callbacks; the helper creates no reference cycles
        collecting = gc.isenabled()
        gc.freeze()
        gc.disable()
        try:
            return self._run()
        finally:
            if collecting:
                gc.enable()
            gc.unfreeze()
            sys.setswitchinterval(switch_interval)

    def install(self) -> bool:
        """Install both low-level hooks, reporting any failure to Que.

        Returns:
            bool: Whether both hooks are installed.
        """
        module = kernel32.GetModuleHandleW(None)
        self.engine.keyboard_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self.keyboard_proc, module, 0
        )
        if not self.engine.keyboard_handle:
            self._send(
                {
                    "type": "error",
                    "text": "Keyboard hook unavailable "
                    f"(Windows error {kernel32.GetLastError()}).",
                }
            )
            return False
        self.engine.mouse_handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self.mouse_proc, module, 0
        )
        if not self.engine.mouse_handle:
            self._send(
                {
                    "type": "error",
                    "text": "Mouse hook unavailable "
                    f"(Windows error {kernel32.GetLastError()}).",
                }
            )
            self.uninstall()
            return False
        return True

    def uninstall(self) -> None:
        """Remove both hooks; Windows may already have removed a timed-out hook."""
        for handle in (self.engine.keyboard_handle, self.engine.mouse_handle):
            if handle:
                user32.UnhookWindowsHookEx(handle)
        self.engine.keyboard_handle = self.engine.mouse_handle = None

    def _run(self) -> int:
        """Install the hooks and pump messages until shutdown, after process setup.

        Returns:
            int: Zero after a clean shutdown, otherwise one.
        """
        writer = threading.Thread(target=self._write_events, daemon=True)
        writer.start()
        if not self._create_monitor_window() or not self.install():
            self.outbox.put(None)
            writer.join(timeout=2)
            return 1
        if not self._register_raw_input():
            self.engine.monitor = None
            self._send(
                {
                    "type": "error",
                    "text": "Que cannot detect keys Windows lets past its keyboard hook "
                    "(Raw Input is unavailable). Shortcuts may leak without warning.",
                }
            )
        threading.Thread(target=self._read_commands, daemon=True).start()
        self._send({"type": "ready"})
        message = ctypes.wintypes.MSG()
        while native.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            native.TranslateMessage(ctypes.byref(message))
            native.DispatchMessageW(ctypes.byref(message))
        self.uninstall()
        self.outbox.put(None)
        writer.join(timeout=2)
        return 0

    def _create_monitor_window(self) -> bool:
        """Create the message-only window that runs the periodic checks and receives Raw Input.

        Returns:
            bool: Whether the window and its timer exist.
        """
        instance = system.GetModuleHandleW(None)
        window_class = _WindowClass()
        window_class.cbSize = ctypes.sizeof(_WindowClass)
        window_class.lpfnWndProc = self.window_proc
        window_class.hInstance = instance
        window_class.lpszClassName = f"QueInputMonitor{os.getpid()}"
        if not native.RegisterClassExW(ctypes.byref(window_class)):
            return False
        self.window = native.CreateWindowExW(
            0,
            window_class.lpszClassName,
            None,
            0,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            None,
            instance,
            None,
        )
        if not self.window or not native.SetTimer(
            self.window, TIMER_ID, TIMER_MS, None
        ):
            self._send(
                {
                    "type": "error",
                    "text": "Que's input helper could not start "
                    f"(Windows error {ctypes.get_last_error()}).",
                }
            )
            return False
        return True

    def _register_raw_input(self) -> bool:
        """Receive Raw Input from every keyboard and mouse, even while another app is active.

        Returns:
            bool: Whether Raw Input is registered.
        """
        devices = (_RawInputDevice * 2)(
            _RawInputDevice(1, 6, RIDEV_INPUTSINK, self.window),
            _RawInputDevice(1, 2, RIDEV_INPUTSINK, self.window),
        )
        return bool(
            native.RegisterRawInputDevices(devices, 2, ctypes.sizeof(_RawInputDevice))
        )

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        """Handle Raw Input and the periodic leak, heartbeat and shutdown check.

        Args:
            hwnd (int): The monitor window.
            message (int): The window message.
            wparam (int): The message's first parameter.
            lparam (int): The message's second parameter.

        Returns:
            int: The message result.
        """
        try:
            if message == WM_INPUT:
                self._on_raw_input(lparam)
            elif message == WM_TIMER and wparam == TIMER_ID:
                self._on_timer()
            elif message == WM_CLOSE:
                native.PostQuitMessage(0)
                return 0
        except BaseException as error:  # noqa: BLE001  # Native callbacks must never unwind into Windows.
            self._send({"type": "error", "text": f"Input monitor error: {error}"})
        return int(native.DefWindowProcW(hwnd, message, wparam, lparam))

    def _on_raw_input(self, handle: int) -> None:
        """Record one physical Raw Input event for comparison with the hooks.

        Args:
            handle (int): The Raw Input data handle.
        """
        monitor = self.engine.monitor
        if monitor is None:
            return
        size = ctypes.wintypes.UINT(0)
        header_size = ctypes.sizeof(_RawInputHeader)
        native.GetRawInputData(handle, RID_INPUT, None, ctypes.byref(size), header_size)
        buffer = ctypes.create_string_buffer(size.value)
        if (
            native.GetRawInputData(
                handle, RID_INPUT, buffer, ctypes.byref(size), header_size
            )
            != size.value
        ):
            return
        header = _RawInputHeader.from_buffer(buffer)
        if not header.hDevice:
            return  # Injected input (including Que's own replays) has no physical device
        tick = int(kernel32.GetTickCount())
        if header.dwType == RIM_TYPEKEYBOARD:
            keyboard = _RawKeyboard.from_buffer(buffer, header_size)
            if keyboard.VKey == 0xFF or keyboard.Flags & RI_KEY_E1:
                return  # Prefix bytes and fake shifts never reach the hook as keys
            down = not keyboard.Flags & RI_KEY_BREAK
            monitor.record_raw(
                key_identity(
                    keyboard.VKey,
                    keyboard.MakeCode,
                    bool(keyboard.Flags & RI_KEY_E0),
                    down,
                ),
                tick,
                f"{_key_name(keyboard.MakeCode, bool(keyboard.Flags & RI_KEY_E0))} "
                f"{'press' if down else 'release'}",
            )
        elif header.dwType == RIM_TYPEMOUSE:
            mouse = _RawMouse.from_buffer(buffer, header_size)
            for flag, (button, down) in RAW_BUTTONS.items():
                if mouse.usButtonFlags & flag:
                    monitor.record_raw(
                        frozenset({("button", button, down)}),
                        tick,
                        f"{BUTTON_NAMES[button]} mouse button "
                        f"{'press' if down else 'release'}",
                    )
            for flag, wheel in RAW_WHEELS.items():
                if mouse.usButtonFlags & flag:
                    monitor.record_raw(
                        frozenset({("wheel", wheel)}), tick, "mouse wheel"
                    )

    def _on_timer(self) -> None:
        """Restore the hooks after any missed event, send heartbeats, and finish shutdown."""
        monitor = self.engine.monitor
        if monitor is not None:
            missed = monitor.expire(int(kernel32.GetTickCount()))
            if missed:
                self.uninstall()
                restored = self.install()
                self._send({"type": "leak", "events": missed, "restored": restored})
        self.ticks += 1
        if self.ticks % HEARTBEAT_TICKS == 0:
            self._send(
                {
                    "type": "heartbeat",
                    "raw_events": monitor.raw_events if monitor is not None else None,
                    "missed_events": monitor.missed_events
                    if monitor is not None
                    else None,
                }
            )
        if not self.connected and self.engine.drained():
            native.PostQuitMessage(0)

    def _read_commands(self) -> None:
        """Apply Que's state updates until Que disconnects."""
        try:
            while True:
                self.engine.apply(json.loads(self.commands.recv_bytes()))
        except (EOFError, OSError, ValueError):
            pass
        # Claim no new input once Que is gone, but keep owning presses until released
        self.engine.release_all_state()
        self.connected = False

    def _write_events(self) -> None:
        """Forward engine events to Que without ever blocking the hook callbacks."""
        while True:
            event = self.outbox.get()
            if event is None:
                return
            try:
                self.events.send_bytes(json.dumps(event).encode("utf-8"))
            except OSError:
                return

    def _send(self, event: dict[str, Any]) -> None:
        """Queue an event for Que.

        Args:
            event (dict[str, Any]): The event to send.
        """
        self.outbox.put(event)


def _key_name(scan: int, extended: bool) -> str:
    """Return the keyboard layout's name for a physical key.

    Args:
        scan (int): The hardware scan code.
        extended (bool): Whether the key has the E0 extended prefix.

    Returns:
        str: The key's name, or its scan code when Windows has no name for it.
    """
    buffer = ctypes.create_unicode_buffer(64)
    length = native.GetKeyNameTextW(
        (scan << 16) | (1 << 24 if extended else 0), buffer, len(buffer)
    )
    return buffer.value if length > 0 else f"key 0x{scan:02X}"


class _WindowClass(ctypes.Structure):
    """Represent WNDCLASSEXW."""

    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", ctypes.wintypes.HICON),
        ("hCursor", ctypes.wintypes.HANDLE),
        ("hbrBackground", ctypes.wintypes.HBRUSH),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ("hIconSm", ctypes.wintypes.HICON),
    ]


class _RawInputDevice(ctypes.Structure):
    """Represent RAWINPUTDEVICE."""

    _fields_ = [
        ("usUsagePage", ctypes.wintypes.USHORT),
        ("usUsage", ctypes.wintypes.USHORT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("hwndTarget", ctypes.wintypes.HWND),
    ]


class _RawInputHeader(ctypes.Structure):
    """Represent RAWINPUTHEADER."""

    _fields_ = [
        ("dwType", ctypes.wintypes.DWORD),
        ("dwSize", ctypes.wintypes.DWORD),
        ("hDevice", ctypes.wintypes.HANDLE),
        ("wParam", ctypes.wintypes.WPARAM),
    ]


class _RawKeyboard(ctypes.Structure):
    """Represent RAWKEYBOARD."""

    _fields_ = [
        ("MakeCode", ctypes.wintypes.USHORT),
        ("Flags", ctypes.wintypes.USHORT),
        ("Reserved", ctypes.wintypes.USHORT),
        ("VKey", ctypes.wintypes.USHORT),
        ("Message", ctypes.wintypes.UINT),
        ("ExtraInformation", ctypes.wintypes.ULONG),
    ]


class _RawMouse(ctypes.Structure):
    """Represent RAWMOUSE, including the alignment padding before its button union."""

    _fields_ = [
        ("usFlags", ctypes.wintypes.USHORT),
        ("padding", ctypes.wintypes.USHORT),
        ("usButtonFlags", ctypes.wintypes.USHORT),
        ("usButtonData", ctypes.wintypes.USHORT),
        ("ulRawButtons", ctypes.wintypes.ULONG),
        ("lLastX", ctypes.wintypes.LONG),
        ("lLastY", ctypes.wintypes.LONG),
        ("ulExtraInformation", ctypes.wintypes.ULONG),
    ]


native.RegisterClassExW.argtypes = [ctypes.POINTER(_WindowClass)]
native.RegisterClassExW.restype = ctypes.wintypes.ATOM
native.CreateWindowExW.argtypes = [
    ctypes.wintypes.DWORD,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.wintypes.HWND,
    ctypes.wintypes.HMENU,
    ctypes.wintypes.HINSTANCE,
    ctypes.wintypes.LPVOID,
]
native.CreateWindowExW.restype = ctypes.wintypes.HWND
native.DefWindowProcW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
native.DefWindowProcW.restype = ctypes.wintypes.LPARAM
native.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(_RawInputDevice),
    ctypes.wintypes.UINT,
    ctypes.wintypes.UINT,
]
native.RegisterRawInputDevices.restype = ctypes.wintypes.BOOL
native.GetRawInputData.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.wintypes.UINT),
    ctypes.wintypes.UINT,
]
native.GetRawInputData.restype = ctypes.wintypes.UINT
native.SetTimer.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_size_t,
    ctypes.wintypes.UINT,
    ctypes.c_void_p,
]
native.SetTimer.restype = ctypes.c_size_t
native.TranslateMessage.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
native.DispatchMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
native.DispatchMessageW.restype = ctypes.wintypes.LPARAM
native.PostQuitMessage.argtypes = [ctypes.c_int]
native.GetKeyNameTextW.argtypes = [
    ctypes.wintypes.LONG,
    ctypes.wintypes.LPWSTR,
    ctypes.c_int,
]
native.GetKeyNameTextW.restype = ctypes.c_int
system.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
system.GetCurrentThread.restype = ctypes.wintypes.HANDLE
system.SetPriorityClass.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
system.SetThreadPriority.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_int]
