"""Tests for the window policy's fail-closed paths.

Every Que window must be excluded from screen capture and unable to take focus before its
first visible frame. When Windows refuses or cannot confirm any of that, the window must stay
hidden. The native surface probe proves the success path on real windows; these tests force
each failure through a fake Win32 layer and check that nothing is shown.
"""

import ctypes
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QWidget
from qt_application import application

from core import window_policy
from core.window_policy import (
    SWP_HIDEWINDOW,
    SWP_SHOWWINDOW,
    WDA_EXCLUDEFROMCAPTURE,
    WDA_NONE,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TRANSPARENT,
    WindowPolicy,
    _CallWindowMessage,
    _WindowPosition,
)

REQUIRED = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
WM_WINDOWPOSCHANGING, WM_NCDESTROY = 0x46, 0x82
HCBT_ACTIVATE, HCBT_SETFOCUS, HCBT_CREATEWND = 5, 9, 3


class FakeUser32:
    """Stand in for user32, with switches to make each protection step fail."""

    def __init__(self) -> None:
        self.style = 0
        self.affinity = WDA_EXCLUDEFROMCAPTURE
        self.style_sticks = True
        self.set_style_fails = False
        self.layered_fails = False
        self.set_affinity_fails = False
        self.get_affinity_fails = False
        self.affinity_sticks = True
        self.next_hook = MagicMock(return_value=42)

    def GetWindowLongPtrW(self, _hwnd: int, _index: int) -> int:
        """Return the window's extended style.

        Returns:
            int: The style bits.
        """
        return self.style

    def SetWindowLongPtrW(self, _hwnd: int, _index: int, style: int) -> int:
        """Apply the extended style unless told to fail.

        Args:
            style (int): The requested style bits.

        Returns:
            int: The previous style, or zero on failure.
        """
        if self.set_style_fails:
            ctypes.set_last_error(5)
            return 0
        previous = self.style
        if self.style_sticks:
            self.style = style
        return previous or 1

    def SetLayeredWindowAttributes(self, *_args: Any) -> int:
        """Make the window opaque-layered unless told to fail.

        Returns:
            int: Whether it succeeded.
        """
        return 0 if self.layered_fails else 1

    def SetWindowDisplayAffinity(self, _hwnd: int, affinity: int) -> int:
        """Record the capture affinity unless told to fail.

        Args:
            affinity (int): The requested affinity.

        Returns:
            int: Whether it succeeded.
        """
        if self.set_affinity_fails:
            return 0
        if self.affinity_sticks:
            self.affinity = affinity
        return 1

    def GetWindowDisplayAffinity(self, _hwnd: int, affinity: Any) -> int:
        """Report the capture affinity unless told to fail.

        Args:
            affinity (Any): A byref DWORD receiving it.

        Returns:
            int: Whether it succeeded.
        """
        if self.get_affinity_fails:
            return 0
        affinity._obj.value = self.affinity
        return 1

    def CallNextHookEx(self, *args: Any) -> int:
        """Pass a hook message on.

        Returns:
            int: The next hook's result.
        """
        return int(self.next_hook(*args))


class FakeComctl32:
    """Stand in for comctl32's window subclassing."""

    def __init__(self) -> None:
        self.subclass_fails = False
        self.removed: list[int] = []

    def SetWindowSubclass(self, *_args: Any) -> int:
        """Install the visibility guard unless told to fail.

        Returns:
            int: Whether it succeeded.
        """
        return 0 if self.subclass_fails else 1

    def RemoveWindowSubclass(self, hwnd: int, *_args: Any) -> int:
        """Record a removed guard.

        Args:
            hwnd (int): The window.

        Returns:
            int: Success.
        """
        self.removed.append(hwnd)
        return 1

    def DefSubclassProc(self, *_args: Any) -> int:
        """Run the default procedure.

        Returns:
            int: A marker value.
        """
        return 9


class PolicyTestCase(unittest.TestCase):
    """Build a policy with native enforcement switched on over the fakes."""

    def setUp(self) -> None:
        """Create the policy and patch its Win32 handles."""
        self.app = application()
        self.user32, self.comctl32 = FakeUser32(), FakeComctl32()
        for name, value in (("native", self.user32), ("controls", self.comctl32)):
            patcher = patch.object(window_policy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.policy = WindowPolicy(self.app)  # Offscreen: installs no real hooks
        self.addCleanup(
            self.policy.setParent, None
        )  # So install_window_policy never reuses it
        self.addCleanup(self.policy.close)
        self.policy.native_enabled = True
        self.failures: list[str] = []
        self.policy.failed.connect(self.failures.append)


class ProtectTests(PolicyTestCase):
    """Refuse to show a window unless every protection is applied and verified."""

    def test_success_applies_styles_and_capture_exclusion(self) -> None:
        """Apply the input styles and capture exclusion, then confirm both."""
        self.assertTrue(self.policy.protect(100))
        self.assertEqual(self.user32.style & REQUIRED, REQUIRED)
        self.assertEqual(self.user32.affinity, WDA_EXCLUDEFROMCAPTURE)
        self.assertIn(100, self.policy.subclassed)
        self.assertEqual(self.failures, [])

    def test_visible_to_capture_when_the_user_chooses_it(self) -> None:
        """Clear capture exclusion only when the setting asks for it."""
        self.policy.capture_hidden = False
        self.assertTrue(self.policy.protect(100))
        self.assertEqual(self.user32.affinity, WDA_NONE)

    def test_every_failed_step_blocks_the_window(self) -> None:
        """Fail closed when any step fails or cannot be verified."""
        for switch, value in (
            ("subclass_fails", True),
            ("set_style_fails", True),
            ("layered_fails", True),
            ("style_sticks", False),
            ("set_affinity_fails", True),
            ("get_affinity_fails", True),
            ("affinity_sticks", False),
        ):
            with self.subTest(switch=switch):
                self.user32, self.comctl32 = FakeUser32(), FakeComctl32()
                self.user32.affinity = WDA_NONE
                target = self.comctl32 if switch == "subclass_fails" else self.user32
                setattr(target, switch, value)
                with (
                    patch.object(window_policy, "native", self.user32),
                    patch.object(window_policy, "controls", self.comctl32),
                ):
                    self.policy.subclassed.clear()
                    self.assertFalse(self.policy.protect(200 + len(self.failures)))
        self.assertEqual(len(self.failures), 7)
        self.assertFalse(self.policy.preparing)

    def test_reentrant_protection_does_not_recurse(self) -> None:
        """Let a nested position change through while the same window is being prepared."""
        self.policy.preparing.add(100)
        self.assertTrue(self.policy.protect(100))
        self.assertEqual(self.user32.style, 0)  # Nothing re-applied

    def test_disabled_policy_protects_nothing(self) -> None:
        """Report no protection once the policy is closed."""
        self.policy.native_enabled = False
        self.assertFalse(self.policy.protect(100))

    def test_errors_are_reported_once(self) -> None:
        """Emit each distinct protection error a single time."""
        self.policy._report("same")
        self.policy._report("same")
        self.assertEqual(self.failures, ["same"])

    def test_capture_setting_reapplies_to_existing_windows(self) -> None:
        """Apply a capture-setting change to every already-created window."""
        widget = QWidget()
        widget.winId()
        self.addCleanup(widget.close)
        with patch.object(self.policy, "protect") as protect:
            self.policy.set_capture_hidden(False)
        protect.assert_any_call(int(widget.winId()))


class NativeCallbackTests(PolicyTestCase):
    """Keep focus out of Que and windows hidden, even when a callback fails."""

    def position(self, flags: int) -> _WindowPosition:
        """Build a proposed window position.

        Args:
            flags (int): SWP_* flags.

        Returns:
            _WindowPosition: The structure.
        """
        return _WindowPosition(0, 0, 0, 0, 0, 0, flags)

    def test_activation_and_focus_are_vetoed(self) -> None:
        """Refuse native activation and focus, and pass every other CBT event on."""
        self.assertEqual(self.policy._on_activation(HCBT_ACTIVATE, 0, 0), 1)
        self.assertEqual(self.policy._on_activation(HCBT_SETFOCUS, 0, 0), 1)
        self.assertEqual(self.policy._on_activation(HCBT_CREATEWND, 0, 0), 42)

    def test_activation_veto_survives_errors(self) -> None:
        """Still refuse activation if passing the event on raises."""
        self.user32.next_hook.side_effect = RuntimeError
        self.assertEqual(self.policy._on_activation(HCBT_CREATEWND, 0, 0), 0)
        with patch.object(window_policy, "native", None):
            self.assertEqual(self.policy._on_activation(HCBT_ACTIVATE, 0, 0), 1)

    def test_show_is_blocked_when_protection_fails(self) -> None:
        """Turn a show into a hide in the window procedure if protection fails."""
        position = self.position(SWP_SHOWWINDOW)
        with patch.object(self.policy, "protect", return_value=False):
            self.assertEqual(
                self.policy._window_proc(
                    1, WM_WINDOWPOSCHANGING, 0, ctypes.addressof(position), 1, 0
                ),
                9,
            )
        self.assertFalse(position.flags & SWP_SHOWWINDOW)
        self.assertTrue(position.flags & SWP_HIDEWINDOW)

    def test_show_is_blocked_when_the_procedure_raises(self) -> None:
        """Hide the window if the guard itself fails."""
        position = self.position(SWP_SHOWWINDOW)
        with patch.object(self.policy, "protect", side_effect=RuntimeError):
            self.assertEqual(
                self.policy._window_proc(
                    1, WM_WINDOWPOSCHANGING, 0, ctypes.addressof(position), 1, 0
                ),
                0,
            )
        self.assertTrue(position.flags & SWP_HIDEWINDOW)

    def test_protected_show_goes_ahead(self) -> None:
        """Let a protected window show, and drop the guard when the window is destroyed."""
        position = self.position(SWP_SHOWWINDOW)
        self.policy.subclassed.add(1)
        self.policy._window_proc(
            1, WM_WINDOWPOSCHANGING, 0, ctypes.addressof(position), 1, 0
        )
        self.assertTrue(position.flags & SWP_SHOWWINDOW)
        self.policy._window_proc(1, WM_NCDESTROY, 0, 0, 1, 0)
        self.assertNotIn(1, self.policy.subclassed)
        self.assertEqual(self.comctl32.removed, [1])

    def test_new_native_windows_are_guarded_before_showing(self) -> None:
        """Guard and protect a native top-level window as Windows proposes showing it."""
        position = self.position(SWP_SHOWWINDOW)
        message = _CallWindowMessage(
            ctypes.addressof(position), 0, WM_WINDOWPOSCHANGING, 300
        )
        with patch.object(self.policy, "protect") as protect:
            self.assertEqual(
                self.policy._on_native_message(0, 0, ctypes.addressof(message)), 42
            )
        self.assertIn(300, self.policy.subclassed)
        protect.assert_called_once_with(300)
        self.assertEqual(
            self.policy._on_native_message(-1, 0, 0), 42
        )  # Negative codes pass straight on
        self.user32.next_hook.side_effect = RuntimeError
        self.assertEqual(self.policy._on_native_message(-1, 0, 0), 0)

    def test_window_id_change_before_creation_protects_nothing(self) -> None:
        """Skip a handle change on a widget that has no native window yet."""
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        with patch.object(self.policy, "protect") as protect:
            self.policy.eventFilter(widget, QEvent(QEvent.Type.WinIdChange))
        protect.assert_not_called()


class HookInstallTests(unittest.TestCase):
    """Refuse to run without the focus and visibility hooks on Windows."""

    def test_missing_hooks_raise(self) -> None:
        """Raise if either GUI-thread hook cannot be installed, removing the first."""
        app = application()
        before = set(app.findChildren(WindowPolicy))

        def detach() -> None:
            """Detach the half-built policies so install_window_policy never reuses them."""
            for policy in set(app.findChildren(WindowPolicy)) - before:
                policy.setParent(None)

        self.addCleanup(detach)
        user32 = MagicMock()
        for results in ([0], [7, 0]):
            with (
                self.subTest(results=results),
                patch.object(window_policy, "native", user32),
                patch.object(QApplication, "platformName", return_value="windows"),
            ):
                user32.SetWindowsHookExW.side_effect = results
                with self.assertRaises(OSError):
                    WindowPolicy(app)
        user32.UnhookWindowsHookEx.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
