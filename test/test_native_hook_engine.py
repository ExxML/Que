"""Tests for the compiled hook engine's Python wrapper: its failure reports and lifecycle.

The equivalence tests in test_native_hook.py prove the compiled decisions match HookEngine.
These cover what the wrapper itself must report: lost input events, a hook thread that stops
taking updates, and an engine that cannot be created. Each means keys may not be hidden.
"""

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from core import native_hook
from core.hook_engine import InputMonitor
from core.native_hook import NativeHookEngine


class NativeEngineTestCase(unittest.TestCase):
    """Create the real compiled engine in its synchronous, uninstalled mode."""

    def setUp(self) -> None:
        """Create the engine and record what it emits."""
        self.events: list[dict[str, Any]] = []
        self.engine = NativeHookEngine(self.events.append)
        self.addCleanup(self.engine.close)


class WrapperTests(NativeEngineTestCase):
    """Report everything that could leave input exposed."""

    def test_monitor_attaches_and_detaches(self) -> None:
        """Hand observations to the Raw Input comparison only while one is attached."""
        monitor = InputMonitor()
        self.engine.monitor = monitor
        self.assertIs(self.engine.monitor, monitor)
        self.engine.monitor = None
        self.assertIsNone(self.engine.monitor)

    def test_quit_waits_until_drained(self) -> None:
        """Answer a quit request once nothing Que owns is still held."""
        self.engine.request_quit()
        self.assertTrue(self.engine.drained())
        self.assertIn("quit_ready", [event["type"] for event in self.events])

    def test_unknown_updates_are_ignored(self) -> None:
        """Ignore a state update the compiled engine does not know."""
        with patch.object(self.engine, "_send") as send:
            self.engine.apply({"type": "not_a_state"})
        send.assert_not_called()

    def test_lost_events_are_reported_once_each(self) -> None:
        """Warn when the native queue overflowed, counting only new losses."""
        stats = {"dropped_events": 3}
        with patch.object(self.engine, "stats", side_effect=lambda: stats):
            self.engine.drain()
            self.engine.drain()
            stats["dropped_events"] = 5
            self.engine.drain()
        errors = [event["text"] for event in self.events if event["type"] == "error"]
        self.assertEqual(len(errors), 2)
        self.assertIn("lost 3 input events", errors[0])
        self.assertIn("lost 2 input events", errors[1])

    def test_stalled_hook_thread_is_reported(self) -> None:
        """Report a hook thread that stops accepting state updates, instead of blocking."""
        library = MagicMock(wraps=self.engine.library)
        library.que_command.return_value = 0
        with (
            patch.object(self.engine, "library", library),
            patch.object(native_hook, "COMMAND_RETRY_S", 0.01),
        ):
            self.engine.apply({"type": "visible", "value": True})
        self.assertEqual(
            self.events[-1],
            {"type": "error", "text": "Que's input helper stopped taking updates."},
        )

    def test_close_stops_a_running_engine(self) -> None:
        """Remove the hooks before freeing a started engine, and free it only once."""
        self.engine.started = True
        with patch.object(
            self.engine,
            "stop",
            side_effect=lambda: setattr(self.engine, "started", False),
        ) as stop:
            self.engine.close()
            self.engine.close()
        stop.assert_called_once()
        self.assertIsNone(self.engine.handle)


class CreationTests(unittest.TestCase):
    """Refuse to run without a native engine."""

    def test_failed_allocation_raises(self) -> None:
        """Raise when the compiled library cannot allocate an engine."""
        library = MagicMock()
        library.que_create.return_value = 0
        with (
            patch("core.native_hook.ctypes.CDLL", return_value=library),
            self.assertRaises(OSError),
        ):
            NativeHookEngine(lambda _event: None)


if __name__ == "__main__":
    unittest.main()
