"""Verify cached fact sheets about linked resume folders and how new chats use them."""

import json
import os
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QPushButton, QWidget
from qt_application import application

from core.ai_sender import AISender
from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.model_catalog import ModelOption
from core.resume_facts import (
    ResumeFacts,
    _sheet_path,
    fingerprint,
    load_sheet,
    sheet_record,
)
from core.settings import SettingsStore, facts_directory
from ui.window.settings_page import SettingsPage


def wait_until(condition: Callable[[], bool], timeout: float = 5) -> bool:
    """Process Qt events until a condition holds or the timeout passes.

    Args:
        condition (Callable[[], bool]): The condition to wait for.
        timeout (float): Seconds to wait.

    Returns:
        bool: Whether the condition held in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application().processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FingerprintTests(unittest.TestCase):
    """Detect folder changes cheaply from git or modification times."""

    def setUp(self) -> None:
        """Create an empty folder."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_git_branch_packed_detached_and_worktree(self) -> None:
        """Read the checked-out commit from loose refs, packed refs, a detached HEAD and a worktree."""
        git = self.root / "repo" / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text("abc\n", encoding="utf-8")
        nested = self.root / "repo" / "tool"
        nested.mkdir()
        # A linked folder inside a repository uses the repository's commit
        self.assertEqual(fingerprint(nested), "git:abc")
        (git / "refs" / "heads" / "main").unlink()
        (git / "packed-refs").write_text(
            "# pack-refs\ndef refs/heads/main\n", encoding="utf-8"
        )
        self.assertEqual(fingerprint(nested), "git:def")
        (git / "HEAD").write_text("123\n", encoding="utf-8")
        self.assertEqual(fingerprint(nested), "git:123")
        worktree_git = git / "worktrees" / "other"
        worktree_git.mkdir(parents=True)
        (worktree_git / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
        (worktree_git / "commondir").write_text("../..", encoding="utf-8")
        worktree = self.root / "other"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {worktree_git}", encoding="utf-8")
        self.assertEqual(fingerprint(worktree), "git:def")

    def test_unreadable_git_falls_back_to_modification_times(self) -> None:
        """Use the newest top-level change when there is no readable commit."""
        folder = self.root / "project"
        (folder / ".git").mkdir(parents=True)
        before = fingerprint(folder)
        self.assertTrue(before.startswith("mtime:"))
        later = time.time() + 100
        (folder / "new.py").write_text("x", encoding="utf-8")
        os.utime(folder / "new.py", (later, later))
        self.assertNotEqual(fingerprint(folder), before)
        self.assertEqual(fingerprint(self.root / "missing"), "")


class ResumeFactsTests(unittest.TestCase):
    """Write, cache and report fact sheets without starting the real CLI."""

    def setUp(self) -> None:
        """Create a linked folder, a file link and a fake live bridge."""
        application()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cache = self.root / "cache"
        self.project = self.root / "Que"
        self.project.mkdir()
        (self.project / "README.md").write_text("Que readme", encoding="utf-8")
        self.notes = self.root / "notes.md"
        self.notes.write_text("Notes", encoding="utf-8")
        self.bridge = MagicMock()
        self.bridge.live = True
        self.bridge.ask.return_value = "  Que is an overlay.  "
        self.facts = ResumeFacts(self.bridge, self.cache)
        self.details = [
            ("Que overlay", str(self.project)),
            ("Notes", str(self.notes)),
            ("Gone", str(self.root / "gone")),
        ]

    def idle(self) -> bool:
        """Return whether no fact sheet is queued or being written.

        Returns:
            bool: Whether the workers finished.
        """
        return not self.facts.queued and self.facts.workers == 0

    def test_write_once_then_rewrite_after_a_change(self) -> None:
        """Write only folders, skip current sheets, and rewrite a changed folder."""
        progress: list[bool] = []
        self.facts.progress.connect(lambda: progress.append(True))
        self.assertEqual(self.facts.status(self.details), (0, 1, 0, ""))
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertTrue(wait_until(self.idle))
        self.assertTrue(wait_until(lambda: bool(progress)))
        self.bridge.ask.assert_called_once()
        prompt, model, effort, folder, _flag = self.bridge.ask.call_args.args
        self.assertIn("Que overlay", prompt)
        self.assertEqual((model, effort, folder), ("sonnet", "high", self.project))
        self.assertEqual(
            load_sheet(self.cache, self.project), ("Que is an overlay.", True)
        )
        self.assertEqual(self.facts.status(self.details), (1, 1, 0, ""))
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(self.bridge.ask.call_count, 1)
        later = time.time() + 100
        os.utime(self.project / "README.md", (later, later))
        self.assertEqual(
            load_sheet(self.cache, self.project), ("Que is an overlay.", False)
        )
        self.facts.refresh(self.details, "", "default")
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(self.bridge.ask.call_count, 2)

    def test_failures_are_reported_and_retried(self) -> None:
        """Report a failed or empty sheet and clear the error when retrying."""
        self.bridge.ask.side_effect = RuntimeError("quota reached")
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(
            self.facts.status(self.details), (0, 1, 0, "Que: quota reached")
        )
        self.bridge.ask.side_effect = None
        self.bridge.ask.return_value = " "
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertTrue(wait_until(self.idle))
        self.assertIn("empty fact sheet", self.facts.status(self.details)[3])
        self.assertIsNone(load_sheet(self.cache, self.project))

    def test_queued_folder_is_not_written_twice(self) -> None:
        """Ignore a refresh for a folder that is already being written, and report it as writing."""
        release = threading.Event()
        self.bridge.ask.side_effect = lambda *_args: (release.wait(5), "Sheet")[1]
        self.facts.refresh(self.details, "sonnet", "high")
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertEqual(self.facts.status(self.details)[2], 1)
        release.set()
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(self.bridge.ask.call_count, 1)

    def test_states_and_rewriting_a_current_sheet(self) -> None:
        """Report each folder state and rewrite a current sheet only when asked."""
        self.assertEqual(self.facts.state(self.project), ("none", ""))
        release = threading.Event()
        self.bridge.ask.side_effect = lambda *_args: (release.wait(5), "Sheet")[1]
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertEqual(self.facts.state(self.project), ("writing", ""))
        release.set()
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(self.facts.state(self.project), ("current", ""))
        self.facts.refresh(self.details, "sonnet", "high", rewrite=True)
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(self.bridge.ask.call_count, 2)
        later = time.time() + 100
        os.utime(self.project / "README.md", (later, later))
        self.assertEqual(self.facts.state(self.project), ("outdated", ""))
        self.bridge.ask.side_effect = RuntimeError("quota reached")
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertTrue(wait_until(self.idle))
        self.assertEqual(
            self.facts.state(self.project), ("failed", "Que: quota reached")
        )

    def test_offline_bridge_never_writes(self) -> None:
        """Never start the CLI outside the running app."""
        self.bridge.live = False
        self.facts.refresh(self.details, "sonnet", "high")
        self.assertEqual(self.facts.workers, 0)
        self.bridge.ask.assert_not_called()

    def test_corrupt_sheets_and_cache_location(self) -> None:
        """Ignore unreadable cache files and keep the cache beside the settings."""
        path = _sheet_path(self.cache, self.project)
        self.cache.mkdir()
        path.write_text("{bad", encoding="utf-8")
        self.assertIsNone(load_sheet(self.cache, self.project))
        path.write_text(json.dumps(["not", "a", "sheet"]), encoding="utf-8")
        self.assertIsNone(load_sheet(self.cache, self.project))
        self.assertIsNone(sheet_record(self.cache, self.project))
        path.write_text(json.dumps({"text": "No fingerprint."}), encoding="utf-8")
        self.assertEqual(
            load_sheet(self.cache, self.project), ("No fingerprint.", False)
        )
        self.assertIsNone(sheet_record(self.cache, self.project))
        store = SettingsStore(self.root / "data" / "config.json")
        self.assertEqual(
            facts_directory(store), self.root / "data" / "cache" / "resume_facts"
        )


class FactSheetPromptTests(unittest.TestCase):
    """Start new chats from fact sheets instead of file reads."""

    def setUp(self) -> None:
        """Create a sender with a linked folder and a cached sheet."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SettingsStore(self.root / "config.json")
        self.sender = AISender(self.store, client=MagicMock())
        self.sender.set_models((ModelOption("m", "M", ("default",)),))
        self.project = self.root / "Que"
        self.project.mkdir()
        (self.project / "README.md").write_text("Que readme", encoding="utf-8")
        self.store.save_resume_details([("Que", str(self.project))])
        bridge = MagicMock()
        bridge.live = True
        bridge.ask.return_value = "Hooks run in C."
        facts = ResumeFacts(bridge, facts_directory(self.store))
        facts.refresh(self.store.resume_details(), "", "default")
        self.assertTrue(wait_until(lambda: facts.workers == 0))

    def test_current_and_stale_sheets_by_provider(self) -> None:
        """Send the sheet to both providers, tell Claude when to open files, and flag stale sheets."""
        gemini = self.sender.prepare_request("Hi").prompt
        self.assertIn("A fact sheet about this folder:\nHooks run in C.", gemini)
        self.assertNotIn("Que readme", gemini)
        self.assertNotIn("read-only file tools", gemini)
        self.sender.provider = "claude"
        claude = self.sender.prepare_request("Hi").prompt
        self.assertIn("Hooks run in C.\n\nAnswer from this fact sheet.", claude)
        self.assertIn("specific detail the sheet lacks.", claude)
        later = time.time() + 100
        os.utime(self.project / "README.md", (later, later))
        stale = self.sender.prepare_request("Hi").prompt
        self.assertIn("written before the folder last changed", stale)
        self.assertIn("or one that may have changed.", stale)


class FactSheetSettingsTests(unittest.TestCase):
    """Show fact sheet progress in Settings and write sheets when folders are linked."""

    def setUp(self) -> None:
        """Create a settings page with isolated storage and offline discovery."""
        self.app = application()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SettingsStore(self.root / "config.json")
        self.store.save("claude", "sonnet", "high", "")
        self.window = QWidget()
        self.addCleanup(self.window.deleteLater)
        bridge = MagicMock(spec=ClaudeBridge, live=False)
        bridge.discover.side_effect = RuntimeError("Fixture Claude discovery failed.")
        self.page = SettingsPage(
            self.store,
            bridge,
            MagicMock(),
            ChatHistory(self.root / "chat_history.json"),
            self.window,
        )
        self.addCleanup(self.page.key_timer.stop)
        self.project = self.root / "Que"
        self.project.mkdir()

    def test_linking_a_folder_writes_only_its_sheet_and_shows_progress(self) -> None:
        """Refresh only the new folder with the saved Claude model and reflect each status."""
        other = self.root / "Other"
        other.mkdir()
        self.store.save_resume_details([("Other", str(other))])
        self.page.open_settings()
        with (
            patch.object(self.page.resume_facts, "refresh") as refresh,
            patch(
                "ui.window.settings_page.choose_directory",
                return_value=str(self.project),
            ),
        ):
            self.page._link_resume_detail(True)
        refresh.assert_called_once_with([("Que", str(self.project))], "sonnet", "high")
        self.assertEqual(
            self.page.fact_sheets_status.text(),
            "Fact sheets: 0 of 2 linked folders are up to date.",
        )
        self.assertFalse(self.page.fact_sheets_button.isEnabled())
        with patch.object(self.page.resume_facts, "status", return_value=(0, 2, 1, "")):
            self.page.resume_facts.progress.emit()
            self.app.processEvents()
            self.assertIn("Writing fact sheets", self.page.fact_sheets_status.text())
        with patch.object(
            self.page.resume_facts, "status", return_value=(0, 2, 0, "Que: quota")
        ):
            self.page._show_fact_sheets()
            self.assertIn(
                "The last one failed: Que: quota", self.page.fact_sheets_status.text()
            )

    def test_folder_marks_show_states_and_write_the_selection(self) -> None:
        """Mark each folder's sheet, and write or rewrite only the selected folders."""
        other = self.root / "Other"
        other.mkdir()
        notes = self.root / "notes.md"
        notes.write_text("Notes", encoding="utf-8")
        details = [
            ("Que", str(self.project)),
            ("Notes", str(notes)),
            ("Other", str(other)),
        ]
        self.store.save_resume_details(details)
        windows = _ShownWindows()
        self.app.installEventFilter(windows)
        self.page.open_settings()
        self.app.removeEventFilter(windows)
        # A row widget shown before joining its row becomes a native window, which Que's
        # window policy keeps native, so it paints black and briefly exists as its own window
        self.assertEqual(windows.shown, [])
        self.assertEqual(set(self.page.fact_marks), {str(self.project), str(other)})
        que_box, que_dot = self.page.fact_marks[str(self.project)]
        other_box, other_dot = self.page.fact_marks[str(other)]
        self.assertFalse(que_box.isHidden())
        self.assertEqual(que_dot.toolTip(), "No fact sheet yet")
        self.assertIn("○", que_dot.text())
        for state, glyph, tooltip in (
            ("current", "●", "Fact sheet is up to date"),
            ("outdated", "●", "Fact sheet was written before the folder last changed"),
            ("failed", "●", "Last write failed: Que: quota"),
            ("writing", "◌", "Writing fact sheet…"),
        ):
            with (
                self.subTest(state=state),
                patch.object(
                    self.page.resume_facts,
                    "state",
                    return_value=(state, "Que: quota" if state == "failed" else ""),
                ),
            ):
                que_box.setChecked(True)
                self.page._show_fact_sheets()
                self.assertIn(glyph, que_dot.text())
                self.assertEqual(que_dot.toolTip(), tooltip)
                self.assertEqual(que_dot.accessibleDescription(), tooltip)
                self.assertEqual(que_box.isEnabled(), state != "writing")
                self.assertEqual(que_box.isChecked(), state != "writing")
                self.assertEqual(
                    self.page.fact_sheets_button.isEnabled(), state != "writing"
                )
        que_box.setChecked(False)
        self.page._show_fact_sheets()
        self.assertFalse(self.page.fact_sheets_button.isEnabled())
        self.page.fact_select_button.click()
        self.assertTrue(que_box.isChecked() and other_box.isChecked())
        self.assertEqual(self.page.fact_select_button.text(), "Clear selection")
        self.page.fact_select_button.click()
        self.assertFalse(que_box.isChecked() or other_box.isChecked())
        self.assertEqual(self.page.fact_select_button.text(), "Select all")
        other_box.setChecked(True)
        with patch.object(self.page.resume_facts, "refresh") as refresh:
            self.page.fact_sheets_button.click()
        refresh.assert_called_once_with(
            [("Other", str(other))], "sonnet", "high", rewrite=True
        )
        self.assertFalse(other_box.isChecked())
        self.assertFalse(self.page.fact_sheets_button.isEnabled())
        self.assertIn("○", other_dot.text())

        with patch.object(self.page.resume_facts, "refresh") as refresh:
            remove = next(
                button
                for button in self.page.findChildren(QPushButton)
                if button.accessibleName() == "Remove link to notes.md"
            )
            remove.click()
            self.page._apply_imported_config({"resume_details"})
        refresh.assert_not_called()
        self.assertEqual(set(self.page.fact_marks), {str(self.project), str(other)})


class _ShownWindows(QObject):
    """Record every top-level window shown while installed on the application."""

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[str] = []

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        """Note a top-level widget being shown and let the event continue.

        Args:
            watched (QObject | None): The object receiving the event.
            event (QEvent | None): The event.

        Returns:
            bool: Always False, so the event is delivered.
        """
        if (
            event is not None
            and event.type() == QEvent.Type.Show
            and isinstance(watched, QWidget)
            and watched.isWindow()
        ):
            self.shown.append(type(watched).__name__)
        return False


if __name__ == "__main__":
    unittest.main()
