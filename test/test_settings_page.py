"""Tests for the settings page's save failures, stale results and input guards.

A setting that fails to save must say so and must not be applied, especially "Hide from
screen recording" and click-through, so Que never claims a protection it did not keep.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PyQt6.QtGui import QWheelEvent
from test_main_window import FLASH, WindowTestCase

from core.provider_catalog import ProviderResult
from core.settings import WHISPER_MODELS
from ui.window.settings_page import _SettingsComboBox

UNWRITABLE = "Check that Que's data folder is writable."
MODEL = WHISPER_MODELS[0]


class SettingsTestCase(WindowTestCase):
    """Open the settings page of a real main window over temporary storage."""

    def setUp(self) -> None:
        """Open settings."""
        super().setUp()
        self.page = self.window.settings_page
        self.page.open_settings()

    def status(self) -> str:
        """Return the save status shown to the user.

        Returns:
            str: The status text.
        """
        return str(self.page.save_status.text())


class ToggleTests(SettingsTestCase):
    """Apply an on/off setting only once it is saved."""

    def test_capture_exclusion_that_fails_to_save_is_not_applied(self) -> None:
        """Show the failure and leave capture exclusion as it was."""
        applied: list[bool] = []
        self.page.capture_hidden_saved.connect(applied.append)
        with patch.object(self.store, "save_capture_hidden", side_effect=OSError):
            self.page.capture_checkbox.toggle()
        self.assertEqual(applied, [])
        self.assertIn("screen recording setting could not be saved", self.status())

    def test_saved_toggles_are_applied(self) -> None:
        """Apply click-through once it is saved."""
        applied: list[bool] = []
        self.page.click_through_saved.connect(applied.append)
        self.page.click_through_checkbox.setChecked(True)
        self.assertEqual(applied, [True])
        self.assertTrue(self.store.click_through())


class SaveFailureTests(SettingsTestCase):
    """Explain every setting that could not be written."""

    def test_unwritable_settings_are_reported(self) -> None:
        """Report each failed save instead of claiming it was saved."""
        cases = (
            (
                "save_claude_directory",
                lambda: self.page._save_directory(str(Path.home())),
            ),
            (
                "save_context_document",
                lambda: self.page._save_document("resume", "cv.txt", "text"),
            ),
            (
                "save_resume_details",
                lambda: self.page._save_resume_details([("Item", str(Path.home()))]),
            ),
            ("save_whisper_model", lambda: self.page._save_whisper_model(MODEL)),
        )
        for method, action in cases:
            with (
                self.subTest(method=method),
                patch.object(self.store, method, side_effect=OSError),
            ):
                action()
                self.assertIn(UNWRITABLE, self.status())

    def test_context_text_saves_only_when_changed(self) -> None:
        """Skip saving unchanged text, and keep a failed save's message after a later success."""
        for method, text_input in (
            ("save_instruction", self.page.instruction_input),
            ("save_job_details", self.page.job_details_input),
        ):
            with self.subTest(method=method):
                with patch.object(self.store, method) as save:
                    self.assertTrue(self.page._save_context_text())
                save.assert_not_called()
                text_input.setPlainText("Be brief.")
                with patch.object(self.store, method, side_effect=OSError):
                    self.assertFalse(self.page._save_context_text())
                self.assertIn(UNWRITABLE, self.status())
        self.page.instruction_input.setPlainText("Changed")
        with patch.object(self.store, "save_instruction", side_effect=OSError):
            self.assertFalse(self.page._save_context_text())
        self.assertIn("instruction could not be saved", self.status())
        self.assertEqual(self.store.job_details(), "Be brief.")

    def test_audio_device_save_failure_restores_the_previous_choice(self) -> None:
        """Put the previous device back when the new choice cannot be saved."""
        combo = self.page.audio_combos["you"]
        combo.blockSignals(True)
        combo.addItem("Headset", "Headset")
        combo.blockSignals(False)
        with patch.object(self.store, "save_audio_device", side_effect=OSError):
            self.page._save_audio_device("you", combo.count() - 1)
        self.assertNotEqual(combo.currentData(), "Headset")
        self.assertIn(UNWRITABLE, self.status())

    def test_downloaded_model_that_cannot_be_selected_is_reported(self) -> None:
        """Report a download error, and a download that could not be selected."""
        self.page._on_whisper_downloaded(MODEL, "offline")
        self.assertIn("Could not download", self.page.download_status.text())
        with patch.object(self.store, "save_whisper_model", side_effect=OSError):
            self.page._on_whisper_downloaded(MODEL, "")
        self.assertIn("could not be selected", self.page.download_status.text())

    def test_screenshot_deletion_needs_two_clicks_and_reports_failure(self) -> None:
        """Arm on the first click, delete on the second, and report undeletable files."""
        with patch.object(
            self.window.chat_history, "delete_screenshots", return_value=False
        ) as delete:
            self.page._delete_screenshots()
            delete.assert_not_called()
            self.page._delete_screenshots()
        delete.assert_called_once()
        self.assertIn("could not be deleted", self.status())
        with patch.object(
            self.window.chat_history, "delete_screenshots", return_value=True
        ):
            self.page._delete_screenshots()
            self.page._delete_screenshots()
        self.assertEqual(self.status(), "Deleted all screenshots.")


class GuardTests(SettingsTestCase):
    """Ignore stale results, unchanged values and cancelled choices."""

    def test_cancelled_choices_change_nothing(self) -> None:
        """Keep everything when a directory or resume link choice is cancelled."""
        with (
            patch("ui.window.settings_page.choose_directory", return_value=""),
            patch.object(self.page, "_save_directory") as save,
            patch.object(self.page, "_save_resume_details") as details,
        ):
            self.page._choose_directory()
            self.page._link_resume_detail(True)
        save.assert_not_called()
        details.assert_not_called()

    def test_chosen_directory_is_saved_and_applied(self) -> None:
        """Save and apply a chosen default directory."""
        applied: list[str] = []
        self.page.directory_saved.connect(applied.append)
        with patch(
            "ui.window.settings_page.choose_directory", return_value=str(Path.home())
        ):
            self.page._choose_directory()
        self.assertEqual(applied, [str(Path.home())])

    def test_unsaved_resume_link_does_not_focus_a_row(self) -> None:
        """Stop when a linked folder cannot be saved."""
        with (
            patch(
                "ui.window.settings_page.choose_directory",
                return_value=str(Path.home()),
            ),
            patch.object(self.page, "_save_resume_details", return_value=False) as save,
        ):
            self.page._link_resume_detail(True)
        save.assert_called_once()

    def test_stale_connection_results_are_ignored(self) -> None:
        """Show only the latest connection check's models or error."""
        self.page.revision = 5
        self.page._on_loaded(4, ProviderResult(self.page.provider, (FLASH,), "old"))
        self.page._on_failed(4, "old error")
        self.assertNotEqual(self.page.status.text(), "old")
        self.assertNotEqual(self.page.status.text(), "old error")

    def test_switching_provider_keeps_the_draft(self) -> None:
        """Keep the shown provider's choice when switching, and ignore re-selecting it."""
        self.page.revision = 1
        self.page._on_loaded(1, ProviderResult(self.page.provider, (FLASH,), "ok"))
        with patch.object(self.page, "_update_provider") as update:
            self.page._select_provider(self.page.provider)
            update.assert_not_called()
            self.page._select_provider("claude")
        self.assertEqual(self.page.drafts["gemini"][0], "flash")

    def test_nothing_saves_without_loaded_models(self) -> None:
        """Save no model or presets before the connection check finishes."""
        self.page.models = ()
        with patch.object(self.store, "save_presets") as presets:
            self.page._save()
            self.page._save_presets()
        presets.assert_not_called()

    def test_unchanged_presets_and_failed_preset_saves(self) -> None:
        """Skip unchanged presets, and report presets that cannot be saved."""
        self.page.revision = 1
        self.page._on_loaded(1, ProviderResult(self.page.provider, (FLASH,), "ok"))
        with patch.object(self.store, "save_presets") as save:
            self.page._save_presets()
        save.reset_mock()
        with (
            patch.object(self.store, "presets", return_value=[]),
            patch.object(self.store, "save_presets", side_effect=OSError),
        ):
            self.page._save_presets()
        self.assertIn(UNWRITABLE, self.status())

    def test_model_without_an_effort_is_not_saved(self) -> None:
        """Wait for an effort level before saving a model choice."""
        self.page.revision = 1
        self.page._on_loaded(1, ProviderResult(self.page.provider, (FLASH,), "ok"))
        self.page.effort_combo.clear()
        with patch.object(self.store, "save") as save:
            self.page._save()
        save.assert_not_called()

    def test_downloaded_whisper_choice_is_saved(self) -> None:
        """Save a downloaded model at once, whatever this machine's model cache holds."""
        model = str(self.page.whisper_combo.currentData())
        self.window.transcriber.downloading = ""
        with (
            patch.object(
                self.window.transcriber, "downloaded_models", return_value=[model]
            ),
            patch.object(self.page, "_save_whisper_model") as save,
        ):
            self.page._on_whisper_changed(self.page.whisper_combo.currentIndex())
        save.assert_called_once_with(model)
        self.assertTrue(self.page.download_status.isHidden())

    def test_whisper_choice_waits_for_a_running_download(self) -> None:
        """Explain nothing new while another model downloads."""
        self.window.transcriber.downloading = "small"
        with (
            patch.object(self.window.transcriber, "downloaded_models", return_value=[]),
            patch.object(self.page, "_show_download_status") as status,
        ):
            self.page._on_whisper_changed(self.page.whisper_combo.currentIndex())
        status.assert_not_called()

    def test_unreadable_settings_are_explained(self) -> None:
        """Show why saved settings could not be read when settings open."""
        self.store.error = "Could not read saved settings."
        self.page.open_settings()
        self.assertEqual(self.page.status.text(), "Could not read saved settings.")

    def test_combo_boxes_ignore_the_wheel(self) -> None:
        """Pass wheel scrolling on instead of changing a setting by accident."""
        event = MagicMock(spec=QWheelEvent)
        _SettingsComboBox().wheelEvent(event)
        event.ignore.assert_called_once()
        _SettingsComboBox().wheelEvent(None)


class ResumeRowTests(SettingsTestCase):
    """Keep resume link rows across reopens unless the links or their files changed."""

    def test_reopening_keeps_unchanged_rows_and_rebuilds_changed_ones(self) -> None:
        """Reuse rows with their selection cleared, and rebuild them once a linked folder is gone."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name) / "Project"
        folder.mkdir()
        self.store.save_resume_details([("Project", str(folder))])
        self.page.open_settings()
        name_input = self.page.detail_rows[0][0]
        checkbox = self.page.fact_marks[str(folder)][0]
        checkbox.setChecked(True)
        self.page.open_settings()
        self.assertIs(self.page.detail_rows[0][0], name_input)
        self.assertFalse(checkbox.isChecked())
        folder.rmdir()
        self.page.open_settings()
        self.assertIsNot(self.page.detail_rows[0][0], name_input)
        self.assertEqual(self.page.fact_marks, {})


if __name__ == "__main__":
    unittest.main()
