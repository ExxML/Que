"""Verify the pre-started Claude CLI, tool-use status and throttled reply rendering."""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.ai_receiver import AIReceiver
from core.ai_sender import AISender
from core.claude_bridge import ClaudeBridge, ClaudeTurn, _tool_status
from core.model_catalog import ModelOption
from core.settings import SettingsStore
from ui.chat.chat_area import RENDER_INTERVAL, ChatArea

CHILD = """import json, sys
p=json.loads(sys.stdin.readline())
events=[
 {'type':'assistant','uuid':'a1','message':{'content':[{'type':'tool_use','name':'Read','input':{'file_path':'C:/x/pkg/app.py'}}]}},
 {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'Done.'}}},
 {'type':'assistant','uuid':'a2','message':{'content':[{'type':'text','text':'Done.'}]}},
 {'type':'result','subtype':'success','is_error':False,'session_id':p['session_id'],'result':'Sheet text'}]
for e in events: print(json.dumps(e),flush=True)
"""
CLAUDE = (ModelOption("sonnet", "Claude Sonnet", ("default", "high")),)


def wait_until(condition: Callable[[], bool], timeout: float = 5) -> bool:
    """Wait for a condition set by a background thread.

    Args:
        condition (Callable[[], bool]): The condition to wait for.
        timeout (float): Seconds to wait.

    Returns:
        bool: Whether the condition held in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class PrewarmTests(unittest.TestCase):
    """Start the next turn's CLI early with a small deterministic child process."""

    def setUp(self) -> None:
        """Create a live bridge whose CLI is a Python child script."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.bridge = ClaudeBridge(self.folder / "neutral", live=True)
        self.addCleanup(self.close)
        for target in (
            patch.object(
                self.bridge,
                "_command",
                return_value=[sys.executable, "-u", "-c", CHILD],
            ),
            patch("core.claude_bridge.WARM_DELAY", 0.01),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.popen = patch(
            "core.claude_bridge.subprocess.Popen", wraps=subprocess.Popen
        )
        self.spawned = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.session = str(uuid4())

    def close(self) -> None:
        """Close the bridge and wait for its processes, which hold the temporary folder open."""
        processes = list(self.bridge.processes)
        self.bridge.close()
        for process in processes:
            process.wait(timeout=5)

    def turn(self, effort: str = "high") -> ClaudeTurn:
        """Describe a resumed turn in the temporary folder.

        Args:
            effort (str): The turn's effort.

        Returns:
            ClaudeTurn: The turn settings.
        """
        return ClaudeTurn("sonnet", effort, self.session, "cp1", self.folder, ())

    def send(self, effort: str = "high", **kwargs: Any) -> str | None:
        """Send a message with the same settings as turn().

        Args:
            effort (str): The turn's effort.
            **kwargs (Any): Extra send_message arguments.

        Returns:
            str | None: The completed assistant UUID.
        """
        return self.bridge.send_message(
            "test",
            (),
            "sonnet",
            effort,
            self.session,
            "cp1",
            self.folder,
            lambda _text: None,
            threading.Event(),
            **kwargs,
        )

    def warm_process(self) -> subprocess.Popen[str]:
        """Wait for the pre-started CLI and return it.

        Returns:
            subprocess.Popen[str]: The waiting process.
        """
        self.assertTrue(wait_until(lambda: self.bridge.warm is not None))
        assert self.bridge.warm is not None
        return self.bridge.warm[2]

    def test_matching_turn_uses_the_waiting_cli_and_reports_tools(self) -> None:
        """Start one hidden CLI for repeated identical requests and use it for the turn."""
        self.bridge.prewarm(self.turn())
        self.bridge.prewarm(self.turn())
        process = self.warm_process()
        self.assertEqual(
            self.spawned.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW
        )
        statuses: list[str] = []
        self.assertEqual(self.send(on_status=statuses.append), "a2")
        self.assertEqual(self.spawned.call_count, 1)
        self.assertIsNotNone(process.poll())
        self.assertEqual(statuses, ["Reading pkg/app.py…"])
        self.assertIsNone(self.bridge.warm)
        self.assertFalse(self.bridge.processes)

    def test_changed_turn_replaces_the_waiting_cli(self) -> None:
        """Close a CLI started for other settings and start a matching one."""
        self.bridge.prewarm(self.turn("default"))
        process = self.warm_process()
        self.assertEqual(self.send(), "a2")
        self.assertEqual(self.spawned.call_count, 2)
        self.assertTrue(wait_until(lambda: process.poll() is not None))
        self.assertTrue(wait_until(lambda: not self.bridge.processes))

    def test_close_none_expiry_and_offline(self) -> None:
        """Close the waiting CLI on None, when idle too long, and at exit; never start one offline."""
        self.bridge.prewarm(self.turn())
        first = self.warm_process()
        self.bridge.prewarm(None)
        self.assertIsNone(self.bridge.warm)
        self.assertTrue(wait_until(lambda: first.poll() is not None))
        with patch("core.claude_bridge.WARM_IDLE", 0.05):
            self.bridge.prewarm(self.turn())
            expired = self.warm_process()
            self.assertTrue(wait_until(lambda: self.bridge.warm is None))
        self.assertTrue(wait_until(lambda: expired.poll() is not None))
        self.bridge.prewarm(self.turn("default"))
        last = self.warm_process()
        self.bridge.close()
        self.assertTrue(wait_until(lambda: last.poll() is not None))
        self.bridge.prewarm(self.turn())
        self.assertIsNone(self.bridge.warm_timer)
        offline = ClaudeBridge(self.folder)
        offline.prewarm(self.turn())
        self.assertIsNone(offline.warm_turn)

    def test_start_skips_stale_missing_and_failed_turns(self) -> None:
        """Stay silent when the turn changed, its directory is gone, Que closed, or the start fails."""
        self.bridge._start_warm(self.turn())  # Not the expected turn
        missing = ClaudeTurn(
            "sonnet", "high", self.session, None, self.folder / "x", ()
        )
        self.bridge.warm_turn = missing
        self.bridge._start_warm(missing)
        self.bridge.warm_turn = self.turn()
        with patch.object(self.bridge, "_spawn", side_effect=OSError):
            self.bridge._start_warm(self.turn())
        self.bridge.closed.set()
        self.bridge._start_warm(self.turn())
        self.assertIsNone(self.bridge.warm)
        self.assertEqual(self.spawned.call_count, 0)

    def test_closing_with_a_claimed_cli_releases_it(self) -> None:
        """Release a waiting CLI that a turn claimed just as Que closed."""
        self.bridge.prewarm(self.turn())
        process = self.warm_process()
        self.bridge.closed.set()
        with self.assertRaisesRegex(RuntimeError, "ended before completing"):
            self.send()
        self.assertTrue(wait_until(lambda: process.poll() is not None))

    def test_release_kills_a_stuck_process_and_ignores_pipe_errors(self) -> None:
        """Kill a process that outlives terminate and tolerate pipe and terminate failures."""
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired("claude", 5), 0]
        process.stdin.close.side_effect = OSError
        process.terminate.side_effect = OSError
        self.bridge._reap(process)
        self.assertTrue(wait_until(lambda: process.kill.called))

    def test_ask_runs_without_a_session_and_leaves_the_waiting_cli(self) -> None:
        """Answer a one-off prompt in a folder without saving or using the chat's CLI."""
        self.bridge.prewarm(self.turn())
        process = self.warm_process()
        self.assertEqual(
            self.bridge.ask(
                "Describe", "sonnet", "high", self.folder, threading.Event()
            ),
            "Sheet text",
        )
        command = self.spawned.call_args.args[0]
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--model=sonnet", command)
        self.assertIn("--effort=high", command)
        self.assertIn("--tools=Read,Grep,Glob", command)
        self.assertEqual(self.spawned.call_args.kwargs["cwd"], self.folder)
        self.assertIsNotNone(self.bridge.warm)
        self.assertIsNone(process.poll())

    def test_ask_reports_failures(self) -> None:
        """Raise when the CLI reports an error or ends without a result."""
        failing = "import sys,json; sys.stdin.readline(); print(json.dumps({'type':'result','subtype':'error','is_error':True,'result':'quota'}),flush=True)"
        silent = "import sys; sys.stdin.readline()"
        for script, message in ((failing, "quota"), (silent, "ended before")):
            with (
                self.subTest(message=message),
                patch.object(
                    self.bridge,
                    "_command",
                    return_value=[sys.executable, "-u", "-c", script],
                ),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                self.bridge.ask(
                    "Describe", "", "default", self.folder, threading.Event()
                )
        command = self.spawned.call_args.args[0]
        self.assertFalse(
            any(part.startswith(("--model", "--effort")) for part in command)
        )

    def test_tool_status_wording(self) -> None:
        """Describe each read-only tool briefly."""
        self.assertEqual(
            _tool_status({"name": "Grep", "input": {"pattern": "def main"}}),
            "Searching for def main…",
        )
        self.assertEqual(
            _tool_status({"name": "Glob", "input": {"pattern": "**/*.py"}}),
            "Finding **/*.py…",
        )
        self.assertEqual(_tool_status({"name": "Read", "input": "bad"}), "Using Read…")
        self.assertEqual(_tool_status({}), "Using a tool…")

    def test_unstreamed_text_is_delivered_once(self) -> None:
        """Show a message's text when the CLI sent it without partial stream events."""
        script = """import json, sys
p=json.loads(sys.stdin.readline())
print(json.dumps({'type':'assistant','uuid':'a1','message':{'content':[{'type':'text','text':'Whole reply'}]}}),flush=True)
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':p['session_id']}),flush=True)
"""
        chunks: list[str] = []
        with patch.object(
            self.bridge, "_command", return_value=[sys.executable, "-u", "-c", script]
        ):
            result = self.bridge.send_message(
                "test",
                (),
                "sonnet",
                "high",
                self.session,
                None,
                None,
                chunks.append,
                threading.Event(),
            )
        self.assertEqual((result, chunks), ("a1", ["Whole reply"]))


class SenderPrewarmTests(unittest.TestCase):
    """Describe the next turn to the bridge whenever its settings change."""

    def setUp(self) -> None:
        """Create a sender whose bridge records pre-start requests."""
        application()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SettingsStore(self.root / "config.json")
        self.sender = AISender(self.store, client=MagicMock())
        self.prewarm = MagicMock()
        self.sender.claude_bridge.prewarm = self.prewarm  # type: ignore[method-assign]

    def test_turn_follows_provider_chat_and_checkpoint(self) -> None:
        """Pre-start a new chat's CLI with the session it will use, then resume at each checkpoint."""
        self.sender.set_models((ModelOption("g", "G", ("default",)),))
        self.prewarm.assert_called_with(None)
        project = self.root / "Que"
        project.mkdir()
        self.store.save_resume_details(
            [("Que", str(project)), ("Notes", str(self.root / "n.md"))]
        )
        self.sender.configure("claude", "")
        self.prewarm.assert_called_with(None)
        self.sender.set_models(CLAUDE)
        self.sender.set_effort_level("high")
        turn = self.prewarm.call_args.args[0]
        self.assertEqual(
            turn,
            ClaudeTurn(
                "sonnet", "high", self.sender.next_session_id, None, None, (project,)
            ),
        )
        first = self.sender.prepare_request("Hi")
        self.assertEqual(first.session_id, turn.session_id)
        self.assertNotEqual(self.sender.next_session_id, turn.session_id)
        self.assertTrue(self.sender.commit_interaction(first, "cp1"))
        resumed = self.prewarm.call_args.args[0]
        self.assertEqual(
            (resumed.session_id, resumed.checkpoint), (first.session_id, "cp1")
        )
        self.sender.restore_chat("cp9", "saved")
        self.assertEqual(self.prewarm.call_args.args[0].checkpoint, "cp9")
        self.sender.reset_chat()
        self.assertIsNone(self.prewarm.call_args.args[0].checkpoint)
        self.sender.set_models(())
        self.prewarm.assert_called_with(None)

    def test_receiver_shows_status_and_prewarms_after_errors_and_stops(self) -> None:
        """Route tool status to the bubble and ready a CLI for the retry."""
        self.sender.configure("claude", "")
        self.sender.set_models(CLAUDE)
        chat = MagicMock()
        chat.streaming_bubble = None
        receiver = AIReceiver(self.sender, chat)

        def send(_request: Any, _on_chunk: Any, stop_flag: Any, on_status: Any) -> None:
            """Report one tool status, then fail.

            Args:
                stop_flag (threading.Event): The cancellation flag.
                on_status (Callable[[str], None]): The status callback.
            """
            on_status("Reading a.py…")
            stop_flag.set()
            on_status("Ignored")
            stop_flag.clear()
            raise RuntimeError("boom")

        with patch.object(self.sender, "send_message", side_effect=send):
            receiver.handle_message("hello")
            assert receiver.ai_thread is not None
            receiver.ai_thread.join(timeout=5)
            self.prewarm.reset_mock()
            application().processEvents()
        chat.start_assistant_stream.assert_called_once_with()
        chat.set_stream_status.assert_called_once_with("Reading a.py…")
        self.prewarm.assert_called()
        receiver._on_response_status("Stale", receiver.stream_id - 1)
        self.assertEqual(chat.set_stream_status.call_count, 1)
        self.prewarm.reset_mock()
        receiver.stop()
        self.prewarm.assert_called()


class ThrottledRenderTests(unittest.TestCase):
    """Redraw a streaming reply at most once per interval without losing text or status."""

    def setUp(self) -> None:
        """Create a chat area with a user message and its loading reply bubble."""
        application()
        self.window = QWidget()
        self.addCleanup(self.window.deleteLater)
        self.chat = ChatArea(self.window)
        self.chat.add_message("Question", is_user=True)
        assert self.chat.streaming_bubble is not None
        self.bubble = self.chat.streaming_bubble

    def text(self) -> str:
        """Return the reply bubble's rendered markup.

        Returns:
            str: The label text.
        """
        return str(self.bubble.message_label.text())

    def test_status_text_and_final_render(self) -> None:
        """Show status with the loading dots, render text at once then throttled, and finish fully."""
        self.chat.set_stream_status("Reading a.py…")
        self.bubble._tick_loading()
        self.assertIn("Reading a.py…", self.text())
        self.chat.append_to_stream("Hello")
        self.assertIn("Hello", self.text())
        self.assertNotIn("Reading a.py…", self.text())
        self.chat.append_to_stream(" world")
        self.assertNotIn("world", self.text())
        self.assertTrue(self.chat.render_timer.isActive())
        QTest.qWait(RENDER_INTERVAL * 3)  # type: ignore[call-arg, arg-type]  # PyQt6 stubs omit @staticmethod
        self.assertIn("Hello world", self.text())
        self.chat.set_stream_status("Finding *.py…")
        self.assertIn("Finding *.py…", self.text())
        self.chat.append_to_stream("!")
        self.chat.finalize_assistant_stream()
        self.assertIn("Hello world!", self.text())
        self.assertNotIn("Finding", self.text())
        self.assertFalse(self.chat.render_timer.isActive())
        self.chat.set_stream_status("Ignored")  # No reply is streaming

    def test_stop_error_and_status_only_reply(self) -> None:
        """Keep unrendered text when stopped, clear status on errors and on an empty reply."""
        self.chat.append_to_stream("Part")
        self.chat.append_to_stream("ial")
        self.chat.stop_assistant_stream()
        self.assertIn("Partial", self.text())
        self.assertIn("Stopped.", self.text())
        self.chat.add_message("Again", is_user=True)
        assert self.chat.streaming_bubble is not None
        self.bubble = self.chat.streaming_bubble
        self.chat.set_stream_status("Reading a.py…")
        self.chat.show_stream_error("Failed")
        self.assertIn("Failed", self.text())
        self.assertNotIn("Reading", self.text())
        self.chat.start_assistant_stream()
        assert self.chat.streaming_bubble is not None
        self.bubble = self.chat.streaming_bubble
        self.chat.set_stream_status("Reading a.py…")
        self.assertIn("Reading a.py…", self.text())
        self.chat.finalize_assistant_stream()
        self.assertNotIn("Reading", self.text())


if __name__ == "__main__":
    unittest.main()
