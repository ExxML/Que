"""Exercise provider isolation, CLI streaming, and encrypted settings offline."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from google.genai import types
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.ai_sender import AISender
from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.gemini_efforts import load_efforts, parse_efforts
from core.model_catalog import EFFORT_WARNING, ModelOption, compatible_model
from core.provider_catalog import ProviderCatalog, ProviderResult
from core.screenshot_manager import ScreenshotManager
from core.settings import SettingsStore
from core.transcriber import Transcriber
from ui.window.main_window import MainWindow
from ui.window.settings_page import SettingsPage

GEMINI = (
    ModelOption("gemini-3-flash-preview", "Gemini Flash", ("default", "low", "high")),
)
CLAUDE = (
    ModelOption("sonnet", "Claude Sonnet", ("default", "low", "high")),
    ModelOption("haiku", "Claude Haiku", ("default",)),
)


class ProviderTests(unittest.TestCase):
    """Verify independent contexts and per-request provider snapshots."""

    def setUp(self) -> None:
        """Create an isolated settings store and disconnected sender."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(Path(self.directory.name) / "config.json")
        self.sender = AISender(self.store, client=MagicMock())
        self.sender.set_models(GEMINI)

    def test_contexts_switch_reset_and_snapshot(self) -> None:
        """Keep provider sessions separate and reject results from previous settings."""
        first = self.sender.prepare_request("Gemini turn")
        self.assertTrue(self.sender.commit_interaction(first, "gemini-context"))
        pending = self.sender.prepare_request("Pending")
        self.sender.configure("claude", "")
        self.sender.set_models(CLAUDE)
        self.assertFalse(self.sender.commit_interaction(pending, "late"))
        self.assertIsNone(self.sender.previous_interaction_id)
        claude_first = self.sender.prepare_request("Claude first")
        self.assertEqual(claude_first.provider, "claude")
        self.assertTrue(
            self.sender.commit_interaction(claude_first, "claude-checkpoint")
        )
        self.sender.set_model("haiku")
        self.assertIsNone(claude_first.claude_directory)
        self.sender.claude_directory = Path(self.directory.name)
        claude_next = self.sender.prepare_request("Claude second")
        self.assertEqual(claude_next.session_id, claude_first.session_id)
        self.assertEqual(claude_next.previous_interaction_id, "claude-checkpoint")
        self.assertEqual(claude_next.claude_directory, Path(self.directory.name))
        self.assertNotEqual(claude_first.claude_directory, claude_next.claude_directory)
        self.sender.configure("gemini", "")
        self.assertEqual(self.sender.previous_interaction_id, "gemini-context")
        self.assertEqual(claude_next.provider, "claude")
        self.sender.configure("claude", "")
        self.assertEqual(self.sender.previous_interaction_id, "claude-checkpoint")
        self.sender.reset_chat()
        self.sender.set_models(CLAUDE)
        fresh = self.sender.prepare_request("New chat")
        self.assertNotEqual(fresh.session_id, claude_first.session_id)
        self.assertIsNone(fresh.previous_interaction_id)
        self.assertEqual(self.sender.contexts, {"gemini": None, "claude": None})

    def test_failed_first_turn_gets_fresh_session(self) -> None:
        """Avoid resuming an uncommitted initial Claude transcript."""
        self.sender.configure("claude", "")
        self.sender.set_models(CLAUDE)
        first = self.sender.prepare_request("Interrupted")
        self.sender.invalidate_requests()
        retry = self.sender.prepare_request("Retry")
        self.assertNotEqual(first.session_id, retry.session_id)
        self.assertFalse(self.sender.commit_interaction(first, "late"))

    def test_key_encryption_roundtrip_and_defaults(self) -> None:
        """Persist both provider defaults while keeping the key out of JSON."""
        self.store.save("gemini", "gemini-model", "high", "test-secret")
        self.store.save("claude", "sonnet", "low", "test-secret")
        self.assertNotIn("test-secret", self.store.path.read_text())
        restored = SettingsStore(self.store.path)
        self.assertEqual(restored.gemini_key(), "test-secret")
        self.assertEqual(restored.defaults("gemini"), ("gemini-model", "high"))
        self.assertEqual(restored.defaults("claude"), ("sonnet", "low"))
        self.assertEqual(restored.provider(), "claude")
        self.assertEqual(restored.claude_directory(), "")
        restored.save_claude_directory(self.directory.name)
        restored.save("claude", "haiku", "default", "test-secret")
        self.assertEqual(
            SettingsStore(self.store.path).claude_directory(), self.directory.name
        )
        with (
            patch("core.settings.os.replace", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            restored.save("gemini", "other", "default", "changed")
        self.assertEqual(SettingsStore(self.store.path).gemini_key(), "test-secret")

    def test_corrupt_settings_do_not_prevent_startup(self) -> None:
        """Expose recoverable errors and use safe defaults for damaged settings."""
        self.store.path.write_text("broken", encoding="utf-8")
        restored = SettingsStore(self.store.path)
        self.assertEqual(restored.provider(), "gemini")
        self.assertTrue(restored.error)
        self.store.path.write_text(json.dumps({"gemini_key": "invalid"}))
        restored = SettingsStore(self.store.path)
        self.assertEqual(restored.gemini_key(), "")
        self.assertIn("Paste it again", restored.error)

    def test_dynamic_efforts_html_markdown_and_unknown(self) -> None:
        """Read live table formats without inventing capabilities for unknown models."""
        html = "<table><tr><th>Model</th><th>Default Thinking</th><th>Levels Supported</th></tr><tr><td><code>gemini-3-flash-preview</code></td><td>On</td><td><code>minimal</code>, high</td></tr></table>"
        markdown = "| Model | Default Thinking | Levels Supported |\n| --- | --- | --- |\n| gemini-3-flash-preview | On | minimal, high |"
        for document in (html, markdown):
            efforts = parse_efforts(document)
            self.assertEqual(efforts["gemini-3-flash-preview"], ("minimal", "high"))
            option = compatible_model(
                types.Model(
                    name="gemini-3-flash-preview", supported_actions=["generateContent"]
                ),
                efforts,
            )
            assert option is not None
            self.assertEqual(option.effort_levels, ("default", "minimal", "high"))
        self.assertEqual(parse_efforts("unrelated documentation"), {})
        option = compatible_model(
            types.Model(name="gemini-9-pro", supported_actions=["generateContent"])
        )
        assert option is not None
        self.assertEqual(option.effort_levels, ("default",))

    def test_efforts_are_fetched_without_caching(self) -> None:
        """Return only the live table, and nothing once the fetch fails."""
        response = MagicMock()
        response.__enter__.return_value.read.return_value = (
            b"| Model | Levels Supported |\n| --- | --- |\n| gemini-2.5-pro | max |"
        )
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(load_efforts(), {"gemini-2.5-pro": ("max",)})
        with patch("urllib.request.urlopen", side_effect=OSError):
            self.assertEqual(load_efforts(), {})

    def test_unreadable_efforts_offer_every_level_with_warning(self) -> None:
        """Keep efforts selectable but flagged when the docs table cannot be read."""
        thinking = types.Model(
            name="gemini-2.5-pro", supported_actions=["generateContent"], thinking=True
        )
        option = compatible_model(thinking, {})
        assert option is not None
        self.assertEqual(
            option.effort_levels, ("default", "minimal", "low", "medium", "high")
        )
        self.assertEqual(option.effort_warning, EFFORT_WARNING)
        option = compatible_model(thinking, {"gemini-2.5-pro": ("low",)})
        assert option is not None
        self.assertEqual(option.effort_levels, ("default", "low"))
        self.assertEqual(option.effort_warning, "")


class ClaudeBridgeTests(unittest.TestCase):
    """Test the actual pipe transport with a small deterministic child process."""

    def setUp(self) -> None:
        """Create a bridge whose working directory is isolated from real sessions."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bridge = ClaudeBridge(Path(self.directory.name))

    def test_model_efforts_follow_cli_metadata(self) -> None:
        """Respect per-model capabilities without deriving them from model names."""
        result = self.bridge.parse_models(
            [
                {
                    "value": "new-model",
                    "displayName": "New Model",
                    "supportedEffortLevels": ["low", "future", "low"],
                },
                {
                    "value": "basic",
                    "supportsEffort": False,
                    "supportedEffortLevels": ["high"],
                },
            ]
        )
        self.assertEqual(result[0].effort_levels, ("default", "low", "future"))
        self.assertEqual(result[1].effort_levels, ("default",))

    def test_stream_and_resume_command(self) -> None:
        """Stream text once, suppress thinking, and pass the accepted checkpoint."""
        session = str(uuid4())
        script = """import json, sys
p=json.loads(sys.stdin.readline())
sys.stderr.write('diagnostic' * 10000)
events=[
 {'type':'stream_event','event':{'delta':{'type':'thinking_delta','thinking':'private'}}},
 {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'Hello'}}},
 {'type':'assistant','uuid':'checkpoint-2','message':{'content':[{'type':'text','text':'Hello'}]}},
 {'type':'result','subtype':'success','is_error':False,'session_id':p['session_id']}]
for e in events: print(json.dumps(e),flush=True)
"""
        chunks: list[str] = []
        with (
            patch.object(
                self.bridge,
                "_command",
                return_value=[sys.executable, "-u", "-c", script],
            ),
            patch(
                "core.claude_bridge.subprocess.Popen", wraps=subprocess.Popen
            ) as popen,
        ):
            result = self.bridge.send_message(
                "test",
                (),
                "sonnet",
                "high",
                session,
                "checkpoint-1",
                Path(self.directory.name),
                chunks.append,
                threading.Event(),
            )
        self.assertEqual(result, "checkpoint-2")
        self.assertEqual(chunks, ["Hello"])
        command = popen.call_args.args[0]
        self.assertIn(f"--resume={session}", command)
        self.assertIn("--resume-session-at=checkpoint-1", command)
        self.assertIn("--effort=high", command)
        self.assertIn("--tools=Read,Grep,Glob", command)
        self.assertEqual(popen.call_args.kwargs["cwd"], Path(self.directory.name))
        self.assertEqual(
            popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW
        )

    def test_tool_turn_separates_messages_and_checks_directory(self) -> None:
        """Keep text from separate tool-use messages apart and reject a deleted directory."""
        script = """import json, sys
p=json.loads(sys.stdin.readline())
events=[
 {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'Looking.'}}},
 {'type':'assistant','uuid':'a1','message':{'content':[{'type':'text','text':'Looking.'}]}},
 {'type':'assistant','uuid':'a2','message':{'content':[{'type':'tool_use'}]}},
 {'type':'stream_event','event':{'delta':{'type':'text_delta','text':'Found it.'}}},
 {'type':'assistant','uuid':'a3','message':{'content':[{'type':'text','text':'Found it.'}]}},
 {'type':'result','subtype':'success','is_error':False,'session_id':p['session_id']}]
for e in events: print(json.dumps(e),flush=True)
"""
        chunks: list[str] = []
        with (
            patch.object(
                self.bridge,
                "_command",
                return_value=[sys.executable, "-u", "-c", script],
            ),
            patch(
                "core.claude_bridge.subprocess.Popen", wraps=subprocess.Popen
            ) as popen,
        ):
            arguments = ("test", (), "sonnet", "default", str(uuid4()), None)
            result = self.bridge.send_message(
                *arguments,
                None,
                chunks.append,
                threading.Event(),
            )
            self.assertEqual(result, "a3")
            self.assertEqual("".join(chunks), "Looking.\n\nFound it.")
            # No directory runs without file tools in the bridge's neutral folder
            self.assertIn("--tools=", popen.call_args.args[0])
            self.assertEqual(popen.call_args.kwargs["cwd"], self.bridge.directory)
            with self.assertRaisesRegex(RuntimeError, "no longer exists"):
                self.bridge.send_message(
                    *arguments,
                    Path(self.directory.name) / "missing",
                    chunks.append,
                    threading.Event(),
                )
        self.assertFalse(self.bridge.lock.locked())

    def test_linked_folders_enable_read_only_tools(self) -> None:
        """Give Claude read-only access to linked folders even without a working directory."""
        script = """import json, sys
p=json.loads(sys.stdin.readline())
print(json.dumps({'type':'assistant','uuid':'a1','message':{'content':[]}}),flush=True)
print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':p['session_id']}),flush=True)
"""
        folder = Path(self.directory.name)
        with (
            patch.object(
                self.bridge,
                "_command",
                return_value=[sys.executable, "-u", "-c", script],
            ),
            patch(
                "core.claude_bridge.subprocess.Popen", wraps=subprocess.Popen
            ) as popen,
        ):
            self.bridge.send_message(
                "test",
                (),
                "sonnet",
                "default",
                str(uuid4()),
                None,
                None,
                lambda _text: None,
                threading.Event(),
                (folder,),
            )
        command = popen.call_args.args[0]
        self.assertIn("--tools=Read,Grep,Glob", command)
        self.assertIn(f"--add-dir={folder}", command)
        self.assertEqual(popen.call_args.kwargs["cwd"], self.bridge.directory)

    def test_cancel_silent_child_and_release_lock(self) -> None:
        """Terminate a stalled CLI promptly even when no response token arrives."""
        flag = threading.Event()
        timer = threading.Timer(0.3, flag.set)
        timer.daemon = True
        script = "import sys,time; sys.stdin.readline(); time.sleep(30)"
        start = time.monotonic()
        with patch.object(
            self.bridge, "_command", return_value=[sys.executable, "-u", "-c", script]
        ):
            timer.start()
            result = self.bridge.send_message(
                "test",
                (),
                "sonnet",
                "default",
                str(uuid4()),
                None,
                Path(self.directory.name),
                lambda _: None,
                flag,
            )
        timer.cancel()
        self.assertIsNone(result)
        self.assertLess(time.monotonic() - start, 4)
        self.assertFalse(self.bridge.lock.locked())

    def test_errors_and_truncation_never_commit(self) -> None:
        """Reject error results and streams that terminate without a result."""
        for event in (
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Usage limit reached",
            },
            {"type": "system"},
        ):
            script = f"import sys; sys.stdin.readline(); print({json.dumps(json.dumps(event))}, flush=True)"
            with (
                patch.object(
                    self.bridge,
                    "_command",
                    return_value=[sys.executable, "-u", "-c", script],
                ),
                self.assertRaises(RuntimeError),
            ):
                self.bridge.send_message(
                    "test",
                    (),
                    "sonnet",
                    "default",
                    str(uuid4()),
                    None,
                    Path(self.directory.name),
                    lambda _: None,
                    threading.Event(),
                )
            self.assertFalse(self.bridge.lock.locked())

    def test_shutdown_terminates_owned_process(self) -> None:
        """End a silent CLI when the application closes and prevent new launches."""
        script = "import sys,time; sys.stdin.readline(); print('{}',flush=True); time.sleep(30)"
        with patch.object(
            self.bridge, "_command", return_value=[sys.executable, "-u", "-c", script]
        ):
            events = self.bridge._events(
                [], {"type": "test"}, threading.Event(), 30, self.bridge.directory
            )
            self.assertEqual(next(events), {})
            process = next(iter(self.bridge.processes))
            self.bridge.close()
            process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)
            events.close()
            self.assertEqual(self.bridge.processes, set())
            self.assertEqual(
                list(
                    self.bridge._events(
                        [], {}, threading.Event(), 1, self.bridge.directory
                    )
                ),
                [],
            )

    def test_screenshot_uses_inline_content(self) -> None:
        """Send image data through stdin and keep user text out of shell commands."""
        image = Path(self.directory.name) / "shot.png"
        image.write_bytes(b"image-data")
        content = self.bridge._content("literal `text` $(command)", (str(image),))
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[0]["source"]["data"], "aW1hZ2UtZGF0YQ==")
        self.assertEqual(content[1]["text"], "literal `text` $(command)")


class SettingsPageTests(unittest.TestCase):
    """Exercise draft isolation, stale check rejection, and saved defaults in Qt."""

    def setUp(self) -> None:
        """Create settings with deterministic connection check identifiers."""
        self.app = application()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(Path(self.directory.name) / "config.json")
        self.store.save("claude", "haiku", "default", "test-key")
        self.store.save("gemini", GEMINI[0].model_id, "high", "test-key")
        self.window = QWidget()
        self.addCleanup(self.window.close)
        self.transcriber = Transcriber(self.store)
        self.transcriber.cache_dir = Path(self.directory.name) / "whisper"
        self.page = SettingsPage(
            self.store,
            ClaudeBridge(),
            self.transcriber,
            ChatHistory(Path(self.directory.name) / "chat_history.json"),
            self.window,
        )
        self.addCleanup(self.page.key_timer.stop)
        refresh = patch.object(self.page.catalog, "refresh", side_effect=range(1, 100))
        refresh.start()
        self.addCleanup(refresh.stop)
        self.page.open_settings()

    def test_switch_only_shows_matching_models_and_defaults(self) -> None:
        """Restore each provider's defaults and discard results from older checks."""
        self.page._on_loaded(1, ProviderResult("gemini", GEMINI, "Connected"))
        self.assertEqual(self.page.effort_combo.currentData(), "high")
        self.page._select_provider("claude")
        self.assertEqual(self.page.model_combo.count(), 0)
        self.page._on_loaded(1, ProviderResult("gemini", GEMINI, "Late"))
        self.assertEqual(self.page.model_combo.count(), 0)
        self.page._on_loaded(2, ProviderResult("claude", CLAUDE, "Connected"))
        self.assertEqual(self.page.model_combo.currentData(), "haiku")
        self.assertEqual(self.page.effort_combo.count(), 1)
        self.assertFalse(self.page.effort_combo.isEnabled())
        self.page._select_provider("gemini")
        self.page._on_loaded(3, ProviderResult("gemini", GEMINI, "Connected"))
        self.assertEqual(self.page.effort_combo.currentData(), "high")

    def test_key_change_is_validated_and_saved_automatically(self) -> None:
        """Debounce key edits and reject old checks before applying the new key."""
        self.page._on_loaded(1, ProviderResult("gemini", GEMINI, "Connected"))
        results: list[ProviderResult] = []
        self.page.saved.connect(results.append)
        self.page.key_input.setText("partial-key")
        self.page.key_input.setText("changed-key")
        self.assertTrue(self.page.key_timer.isActive())
        self.page._on_loaded(1, ProviderResult("gemini", GEMINI, "Late"))
        self.assertEqual(results, [])
        self.assertEqual(SettingsStore(self.store.path).gemini_key(), "test-key")
        QTest.qWait(650)  # type: ignore[call-arg, arg-type]  # PyQt6 stubs omit @staticmethod
        self.assertEqual(self.page.revision, 2)
        self.page._on_loaded(2, ProviderResult("gemini", GEMINI, "Connected"))
        self.assertEqual(len(results), 1)
        self.assertEqual(SettingsStore(self.store.path).gemini_key(), "changed-key")
        self.page.open_settings()
        self.assertEqual(self.page.key_input.text(), "changed-key")

    def test_saved_defaults_survive_reopen(self) -> None:
        """Save verified selections and restore them on the next settings visit."""
        self.page._select_provider("claude")
        self.page._on_loaded(2, ProviderResult("claude", CLAUDE, "Connected"))
        self.page.model_combo.setCurrentIndex(0)
        self.page.effort_combo.setCurrentIndex(2)
        self.assertEqual(self.store.defaults("claude"), ("sonnet", "high"))
        self.page.open_settings()
        self.page._on_loaded(3, ProviderResult("claude", CLAUDE, "Connected"))
        self.assertEqual(self.page.model_combo.currentData(), "sonnet")
        self.assertEqual(self.page.effort_combo.currentData(), "high")

    def test_refresh_does_not_reapply_unchanged_settings(self) -> None:
        """Avoid overwriting chat overrides merely by opening settings."""
        results: list[ProviderResult] = []
        self.page.saved.connect(results.append)
        self.page._on_loaded(1, ProviderResult("gemini", GEMINI, "Connected"))
        self.assertEqual(results, [])
        self.page.effort_combo.setCurrentIndex(1)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.store.defaults("gemini")[1], "low")

    def test_failed_key_is_not_saved_when_switching_to_claude(self) -> None:
        """Keep the last verified Gemini key when selecting another provider."""
        self.page.key_input.setText("invalid-key")
        self.page._refresh(False)
        self.page._on_failed(2, "Invalid key")
        self.assertEqual(self.store.gemini_key(), "test-key")
        self.page._select_provider("claude")
        self.page._on_loaded(3, ProviderResult("claude", CLAUDE, "Connected"))
        self.assertEqual(self.store.provider(), "claude")
        self.assertEqual(self.store.gemini_key(), "test-key")

    def test_write_failure_does_not_apply_settings(self) -> None:
        """Report storage errors without applying unsaved defaults."""
        self.page._on_loaded(1, ProviderResult("gemini", GEMINI, "Connected"))
        results: list[ProviderResult] = []
        self.page.saved.connect(results.append)
        with patch.object(self.store, "save", side_effect=OSError):
            self.page.effort_combo.setCurrentIndex(1)
        self.assertEqual(results, [])
        self.assertIn("could not be saved", self.page.save_status.text())
        self.assertEqual(self.store.defaults("gemini")[1], "high")

    def test_whisper_model_is_saved_only_once_downloaded(self) -> None:
        """Offer to download a missing model and select it once the download finishes."""
        combo = self.page.whisper_combo
        self.assertEqual(combo.currentData(), "small.en")
        self.assertTrue(self.page.download_button.isEnabled())
        combo.setCurrentIndex(combo.findData("tiny.en"))
        self.assertNotIn("whisper_model", self.store.data)
        with patch.object(self.transcriber, "_download"):
            self.page._download_whisper()
        self.assertFalse(self.page.download_button.isEnabled())
        snapshot = (
            self.transcriber.cache_dir
            / "models--Systran--faster-whisper-tiny.en"
            / "snapshots"
            / "abc"
        )
        snapshot.mkdir(parents=True)
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (snapshot / name).write_text("x")
        self.transcriber.download_finished.emit("tiny.en", "")
        self.assertEqual(self.store.whisper_model(), "tiny.en")
        self.assertEqual(combo.currentData(), "tiny.en")
        self.assertIn("downloaded", combo.currentText())
        self.assertEqual(self.page.download_button.text(), "Downloaded")
        self.assertFalse(self.page.download_button.isEnabled())
        combo.setCurrentIndex(combo.findData("small.en"))
        self.assertEqual(self.store.whisper_model(), "tiny.en")
        self.assertTrue(self.page.download_button.isEnabled())

    def test_auto_apply_keeps_page_open_and_does_not_cancel_model_changes(self) -> None:
        """Apply effort changes to subsequent turns without interrupting the current one."""
        with patch("core.provider_catalog.ProviderCatalog.refresh", return_value=1):
            sender = AISender(self.store)
            window = MainWindow(sender, ScreenshotManager())
            self.addCleanup(window.close)
            window.visibility_timer.stop()
            window._on_models_loaded(1, ProviderResult("gemini", GEMINI, "Connected"))
            window.show_settings()
            window.settings_page._on_loaded(
                1, ProviderResult("gemini", GEMINI, "Connected")
            )
            pending = sender.prepare_request("Current turn")
            window.settings_page.effort_combo.setCurrentIndex(1)
            self.assertEqual(sender.effort_level, "low")
            self.assertEqual(pending.effort_level, "high")
            self.assertEqual(sender.generation, pending.generation)
            self.assertEqual(window.pages.currentIndex(), 1)
            self.assertTrue(
                window.input_field.input_settings.directory_widget.isHidden()
            )

    def test_catalog_redacts_key_errors(self) -> None:
        """Keep credential values out of connection error messages."""
        catalog = ProviderCatalog(ClaudeBridge())
        errors: list[str] = []
        catalog.failed.connect(lambda _revision, error: errors.append(error))
        with patch(
            "core.provider_catalog.genai.Client",
            side_effect=RuntimeError("bad test-key"),
        ):
            catalog._load(1, "gemini", "test-key", False)
        self.assertEqual(errors, ["bad [API key]"])


if __name__ == "__main__":
    unittest.main()
