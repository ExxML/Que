"""Tests for supervising the input helper, which hides Que's keys and clicks from other apps.

If the helper dies, goes silent or cannot be elevated, Que must tell the user that input is
exposed and start a new helper. Messages from a replaced helper must never act on the new one.
"""

import ctypes
import subprocess
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from qt_application import application

from core import hook_client
from core.hook_client import (
    STILL_ACTIVE,
    ElevatedProcess,
    HookClient,
    hook_host_command,
)


class FakeConnection:
    """A pipe end that replays queued messages, then reports the peer gone."""

    def __init__(self, incoming: list[bytes] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.sent: list[bytes] = []
        self.closed = False

    def recv_bytes(self) -> bytes:
        """Return the next queued message.

        Returns:
            bytes: The message.
        """
        if not self.incoming:
            raise EOFError
        return self.incoming.pop(0)

    def send_bytes(self, data: bytes) -> None:
        """Record a sent message.

        Args:
            data (bytes): The message.
        """
        self.sent.append(data)

    def close(self) -> None:
        """Record that the pipe was closed."""
        self.closed = True


class ClientTestCase(unittest.TestCase):
    """Build a client that never starts a real helper."""

    def setUp(self) -> None:
        """Create the client and record its signals."""
        application()
        self.client = HookClient()
        self.alerts: list[str] = []
        self.client.alert.connect(self.alerts.append)
        spawn = patch.object(self.client, "_spawn")
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)


class DispatchTests(ClientTestCase):
    """Turn helper events into Qt signals, and restart a helper that went away."""

    def test_pointer_and_armed_events_track_the_latest_press(self) -> None:
        """Relay pointer input and typing mode, keeping the newest press sequence."""
        pointers: list[tuple[int, ...]] = []
        armed: list[bool] = []
        self.client.pointer_input.connect(lambda *args: pointers.append(args))
        self.client.armed_changed.connect(armed.append)
        self.client.dispatch(
            {
                "type": "pointer",
                "held": [0xA0],
                "seq": 4,
                "message": 0x201,
                "x": 1,
                "y": 2,
                "data": 0,
                "hwnd": 9,
            }
        )
        self.client.dispatch({"type": "armed", "seq": 3, "value": True})
        self.assertEqual(pointers, [(0x201, 1, 2, 0, 9)])
        self.assertEqual(armed, [True])
        self.assertEqual(
            (self.client.held_modifiers, self.client.last_press_seq), ((0xA0,), 4)
        )

    def test_ready_helper_resolves_earlier_outages(self) -> None:
        """Clear each outage warning once a new helper connects."""
        resolved: list[str] = []
        self.client.resolved.connect(resolved.append)
        self.client._alert("Helper stopped")
        self.client._alert("Helper stopped")
        self.client.dispatch({"type": "ready"})
        self.assertTrue(self.client.connected)
        self.assertEqual(resolved, ["Helper stopped"])
        self.assertEqual(self.client.outages, [])

    def test_lost_helper_alerts_and_restarts(self) -> None:
        """Warn that input is exposed and start a new helper when the current one disconnects."""
        self.client.connected = True
        self.client.dispatch(
            {"type": "disconnected", "generation": self.client.generation}
        )
        self.assertFalse(self.client.connected)
        self.assertTrue(self.alerts and "not hidden" in self.alerts[0])
        self.spawn.assert_called_once()

    def test_old_helpers_and_shutdown_are_ignored(self) -> None:
        """Ignore a replaced helper's disconnect or declined prompt, and anything while stopping."""
        self.client.generation = 5
        self.client.dispatch({"type": "disconnected", "generation": 4})
        self.client.dispatch({"type": "elevation_declined", "generation": 4})
        self.client.stopping = True
        self.client.dispatch({"type": "disconnected", "generation": 5})
        self.spawn.assert_not_called()
        self.assertEqual(self.alerts, [])

    def test_declined_elevation_restarts_unelevated(self) -> None:
        """Start an ordinary helper when the user declines the UAC prompt."""
        self.client.dispatch(
            {"type": "elevation_declined", "generation": self.client.generation}
        )
        self.spawn.assert_called_once()

    def test_quit_without_a_helper_is_immediate(self) -> None:
        """Quit at once when no helper is connected to wait for."""
        self.assertFalse(self.client.request_quit())

    def test_watchdog_does_nothing_while_stopping(self) -> None:
        """Never restart the helper during shutdown."""
        self.client.stopping = True
        self.client._check_helper()
        self.spawn.assert_not_called()


class PipeTests(ClientTestCase):
    """Send state to the helper and relay its events over the authenticated pipes."""

    def test_stop_closes_the_command_pipe(self) -> None:
        """Close the pipe on stop, so the helper releases its hooks."""
        pipe = FakeConnection()
        self.client.commands = pipe  # type: ignore[assignment]
        self.client.stop()
        self.assertTrue(pipe.closed)
        self.assertIsNone(self.client.commands)

    def test_broken_command_pipe_is_dropped(self) -> None:
        """Forget a pipe that fails to send; state is re-sent on reconnection."""
        pipe = MagicMock()
        pipe.send_bytes.side_effect = OSError
        self.client.commands = pipe
        self.client.set_visible(True)
        self.assertIsNone(self.client.commands)
        self.assertEqual(self.client.state["visible"]["value"], True)

    def test_connect_sends_state_relays_events_then_reports_disconnect(self) -> None:
        """Replay current state to a new helper, relay its events, and report when it goes."""
        self.client.set_visible(True)
        commands, events = (
            FakeConnection(),
            FakeConnection([b'{"type": "action", "name": "new_chat"}']),
        )
        listener = MagicMock()
        listener.accept.side_effect = [commands, events]
        received: list[dict[str, Any]] = []
        self.client.received.disconnect()
        self.client.received.connect(received.append)
        self.client._connect(listener, self.client.generation)
        self.assertEqual(commands.sent, [b'{"type": "visible", "value": true}'])
        self.assertEqual(
            received,
            [
                {"type": "action", "name": "new_chat"},
                {"type": "disconnected", "generation": self.client.generation},
            ],
        )
        listener.close.assert_called_once()

    def test_connect_for_a_replaced_helper_closes_its_pipes(self) -> None:
        """Close a late-connecting old helper's pipes so it exits."""
        commands, events = FakeConnection(), FakeConnection()
        listener = MagicMock()
        listener.accept.side_effect = [commands, events]
        self.client._connect(listener, self.client.generation - 1)
        self.assertTrue(commands.closed and events.closed)
        self.assertIsNone(self.client.commands)

    def test_connect_gives_up_when_the_helper_never_connects(self) -> None:
        """Stop waiting when the pipe fails before the helper connects."""
        listener = MagicMock()
        listener.accept.side_effect = OSError
        self.client._connect(listener, self.client.generation)
        listener.close.assert_called_once()


class SpawnTests(unittest.TestCase):
    """Start helpers, replacing any previous one."""

    def setUp(self) -> None:
        """Create a client with process creation faked."""
        application()
        self.client = HookClient()
        self.alerts: list[str] = []
        self.client.alert.connect(self.alerts.append)

    def test_spawn_replaces_a_running_helper(self) -> None:
        """Kill the previous helper and close its pipe before starting the next."""
        old, pipe = MagicMock(), FakeConnection()
        old.poll.return_value = None
        self.client.process, self.client.commands = old, pipe  # type: ignore[assignment]
        with (
            patch.object(hook_client, "Listener"),
            patch("core.hook_client.subprocess.Popen") as popen,
            patch("core.hook_client.threading.Thread"),
        ):
            self.client._spawn()
        old.kill.assert_called_once()
        self.assertTrue(pipe.closed)
        popen.assert_called_once()

    def test_spawn_failure_alerts(self) -> None:
        """Warn that input is exposed when no helper can be started."""
        with patch.object(hook_client, "Listener", side_effect=OSError("no pipes")):
            self.client._spawn()
        self.assertEqual(self.alerts, ["Que's input helper could not start: no pipes"])

    def test_elevated_launch_declined_reports_it(self) -> None:
        """Close the listener and report a declined UAC prompt."""
        received: list[dict[str, Any]] = []
        self.client.received.disconnect()
        self.client.received.connect(received.append)
        listener = MagicMock()
        with patch.object(hook_client, "ElevatedProcess", side_effect=OSError):
            self.client._launch_elevated(listener, "pipe", b"key", 3)
        listener.close.assert_called_once()
        self.assertEqual(received, [{"type": "elevation_declined", "generation": 3}])

    def test_elevated_launch_for_a_replaced_helper_is_not_kept(self) -> None:
        """Keep an elevated process only if it is still the current helper."""
        with (
            patch.object(hook_client, "ElevatedProcess") as elevated,
            patch.object(self.client, "_connect") as connect,
        ):
            self.client._launch_elevated(
                MagicMock(), "pipe", b"key", self.client.generation + 1
            )
            self.assertIsNone(self.client.process)
            self.client._launch_elevated(
                MagicMock(), "pipe", b"key", self.client.generation
            )
            self.assertIs(self.client.process, elevated.return_value)
        self.assertEqual(connect.call_count, 2)
        self.assertIn(b"key".hex(), elevated.call_args.args[0])

    def test_compiled_builds_host_the_helper_themselves(self) -> None:
        """Start the helper from the executable itself in a Nuitka build."""
        with patch.dict(hook_client.__dict__, {"__compiled__": True}):
            self.assertEqual(hook_host_command("pipe")[1:], ["--hook-host", "pipe"])


class ElevatedProcessTests(unittest.TestCase):
    """Wrap a UAC-started process in the Popen calls Que uses."""

    def setUp(self) -> None:
        """Fake the shell and kernel calls."""
        self.shell, self.kernel = MagicMock(), MagicMock()
        for name, value in (("_shell32", self.shell), ("_kernel32", self.kernel)):
            patcher = patch.object(hook_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_declined_prompt_raises(self) -> None:
        """Raise when the user declines the prompt."""
        self.shell.ShellExecuteExW.return_value = 0
        with self.assertRaises(OSError):
            ElevatedProcess(["que.exe", "--hook-host", "pipe"])

    def test_poll_kill_and_wait(self) -> None:
        """Report running, exited and unknown states, kill, and time out a wait."""

        def start(info: Any) -> int:
            """Pretend the prompt was accepted.

            Args:
                info (Any): A byref SHELLEXECUTEINFOW.

            Returns:
                int: Success.
            """
            info._obj.hProcess = 55
            return 1

        self.shell.ShellExecuteExW.side_effect = start
        process = ElevatedProcess(["que.exe", "--hook-host", "pipe"])
        self.assertEqual(process.handle, 55)
        codes = [STILL_ACTIVE, 3]

        def exit_code(_handle: int, code: Any) -> int:
            """Report the next exit code.

            Args:
                code (Any): A byref DWORD receiving it.

            Returns:
                int: Success.
            """
            code._obj.value = codes.pop(0)
            return 1

        self.kernel.GetExitCodeProcess.side_effect = exit_code
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(0.01)
        self.assertEqual(process.poll(), 3)
        self.kernel.GetExitCodeProcess.side_effect = None
        self.kernel.GetExitCodeProcess.return_value = 0
        self.assertIsNone(process.poll())
        process.kill()
        self.kernel.TerminateProcess.assert_called_once_with(55, 1)
        self.assertIsInstance(ctypes.sizeof(hook_client._ShellExecuteInfo), int)


if __name__ == "__main__":
    unittest.main()
