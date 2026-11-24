"""Exercise backup files, selective import and their production UI entry points."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PyQt6.QtWidgets import QFileDialog
from qt_application import application

from core.ai_sender import AISender
from core.chat_history import ChatHistory, Conversation
from core.model_catalog import ModelOption
from core.provider_catalog import ProviderResult
from core.screenshot_manager import ScreenshotManager
from core.settings import CONFIG_KEYS, WHISPER_MODELS, SettingsStore
from ui.window.main_window import MainWindow

KEY = "synthetic-gemini-key"
ENCRYPTED = b"synthetic DPAPI blob"
PICKER = "ui.window.config_panel.choose_file"
FLASH = ModelOption("flash", "Flash", ("low", "high"))
MODEL = WHISPER_MODELS[0]


class ConfigTransferTests(unittest.TestCase):
    """Use the settings tab with temporary settings, history, and screenshot data."""

    def setUp(self) -> None:
        """Open the Config tab and place backup files outside the live settings folder."""
        self.app = application()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = SettingsStore(root / "data" / "config.json")
        self.history = ChatHistory(root / "data" / "history.json")
        self.screenshots = ScreenshotManager()
        self.screenshots.screenshots_dir = self.history.screenshots_dir
        self.screenshots.screenshots_dir.mkdir(parents=True)
        with (
            patch("core.provider_catalog.ProviderCatalog.refresh", return_value=1),
            patch("ui.window.main_window.ChatHistory", return_value=self.history),
        ):
            self.window = MainWindow(AISender(self.store), self.screenshots)
        self.addCleanup(self.window.close)
        self.window.visibility_timer.stop()
        self.sender = self.window.ai_sender
        self.backup = root / "settings.json"
        self.page = self.window.settings_page
        self.page.open_settings()
        self.page.tab_bar.setCurrentIndex(self.page.tab_bar.count() - 1)
        self.panel = self.page.config_panel
        protect = patch(
            "core.settings.win32crypt.CryptProtectData", return_value=ENCRYPTED
        )
        unprotect = patch(
            "core.settings.win32crypt.CryptUnprotectData",
            side_effect=self.decrypt_fixture,
        )
        protect.start()
        unprotect.start()
        self.addCleanup(protect.stop)
        self.addCleanup(unprotect.stop)
        self.assertEqual(
            self.page.tab_bar.tabText(self.page.tab_bar.currentIndex()), "Config"
        )
        self.assertTrue(self.page.save_status.isHidden())

    @staticmethod
    def decrypt_fixture(blob: bytes, *_arguments: object) -> tuple[str, bytes]:
        """Simulate account-bound encryption without using real Windows credentials."""
        if blob != ENCRYPTED:
            raise ValueError("Fixture key belongs to another account.")
        return "", KEY.encode("utf-8")

    def load_models(self) -> None:
        """Install the synthetic catalog on the chat window."""
        self.window._apply_models(ProviderResult(self.sender.provider, (FLASH,), ""))

    def write_backup(self, settings: dict[str, Any], version: int = 1) -> None:
        """Create a Que backup with the specified values."""
        self.backup.write_text(
            json.dumps(
                {"format": "que-settings", "version": version, "settings": settings}
            ),
            encoding="utf-8",
        )

    def open_backup(self) -> None:
        """Choose the fixture through the import button."""
        with patch(PICKER, return_value=str(self.backup)):
            self.panel.browse_button.click()

    def test_export_round_trips_preferences_and_excludes_private_activity(self) -> None:
        """Export all preferences, retaining settings data and a Windows-encrypted key."""
        self.store.save("claude", "sonnet", "high", KEY)
        self.store.save("gemini", "flash", "low", KEY)
        self.store.save_presets("gemini", [("flash", "high")])
        self.store.save_context_document("resume", "cv.txt", "Synthetic résumé")
        self.store.save_resume_details([("Original", str(self.backup))])
        self.page._show_resume_details()
        self.page.detail_rows[0][0].setText("Edited")
        self.page.instruction_input.setPlainText("Recently typed instruction")
        self.store.save_audio_device("you", "USB microphone")
        self.store.save_whisper_model(MODEL)
        self.store.save_claude_directory(str(self.backup.parent))
        self.store.save_click_through(True)
        self.history.save(
            Conversation(
                "gemini",
                "Private conversation",
                messages=[{"text": "Private transcript"}],
            )
        )
        screenshot = self.screenshots.screenshots_dir / "private.png"
        screenshot.write_bytes(b"Private screenshot")
        self.store.data["future_runtime_data"] = "Private unknown data"

        with patch(PICKER, return_value=str(self.backup)) as pick:
            self.panel.export_button.click()
        self.assertEqual(pick.call_args.kwargs["save_as"], "que-settings.json")
        contents = self.backup.read_text(encoding="utf-8")
        self.assertNotIn(KEY, contents)
        self.assertNotIn("Private", contents)
        self.assertNotIn("private.png", contents)
        values, key_unavailable = self.store.read_config(self.backup)
        self.assertFalse(key_unavailable)
        self.assertEqual(values.keys(), CONFIG_KEYS)
        self.assertEqual(values["instruction"], "Recently typed instruction")
        self.assertEqual(values["resume_details"][0]["name"], "Edited")

        restored = SettingsStore(self.store.path.with_name("restored.json"))
        restored.import_config(values)
        self.assertEqual(restored.config_values(), self.store.config_values())
        self.assertEqual(restored.gemini_key(), KEY)
        self.assertIn("Settings exported", self.panel.status.text())

    def test_selective_import_is_atomic_and_updates_controls(self) -> None:
        """Apply the chosen Context, Audio, Voice and Window values after one saved write."""
        self.store.save_instruction("Original")
        self.page.instruction_input.setPlainText("Original")
        self.store.save_context_document("resume", "old.txt", "Old résumé")
        self.store.save_context_document("cover_letter", "letter.txt", "Keep me")
        self.store.save_audio_device("them", "Keep speakers")
        # Que ships one model, so add a second one for the backup to differ from
        models = patch("core.settings.WHISPER_MODELS", (MODEL, "other-model"))
        models.start()
        self.addCleanup(models.stop)
        self.store.save_whisper_model("other-model")
        self.history.save(Conversation("gemini", "Existing conversation"))
        original_history = self.history.path.read_bytes()
        self.write_backup(
            {
                "instruction": "Imported",
                "job_details": "Imported role",
                "context_documents.resume": None,
                "context_documents.cover_letter": {
                    "name": "new.txt",
                    "text": "Decline me",
                },
                "resume_details": [{"name": "Example", "path": str(self.backup)}],
                "claude_directory": str(self.backup.parent),
                "audio_device_you": "Imported microphone",
                "audio_device_them": "",
                "whisper_model": MODEL,
                "system_tray": False,
                "capture_hidden": False,
                "click_through": True,
                "elevate_helper": True,
                "transcript": "Unknown values are ignored",
            }
        )
        self.open_backup()
        self.assertFalse(self.panel.apply_button.isEnabled())
        self.assertNotIn("transcript", self.panel.choices)
        self.panel.select_button.click()
        self.assertEqual(self.panel.select_button.text(), "Clear selection")
        self.panel.select_button.click()
        self.assertFalse(self.panel.apply_button.isEnabled())
        self.panel.select_button.click()
        for key in (
            "audio_device_them",
            "context_documents.cover_letter",
            "capture_hidden",
        ):
            self.panel.choices[key].setChecked(False)
        toggles: list[bool] = []
        self.page.click_through_saved.connect(toggles.append)
        original = self.store.path.read_bytes()
        with patch("core.settings.os.replace", side_effect=PermissionError):
            self.panel.apply_button.click()
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(toggles, [])
        self.assertIn("Could not save", self.panel.status.text())

        with patch.object(self.store, "_write", wraps=self.store._write) as write:
            self.panel.apply_button.click()
        write.assert_called_once()
        self.assertEqual(toggles, [True])
        self.assertEqual(self.page.instruction_input.toPlainText(), "Imported")
        self.assertEqual(self.page.job_details_input.toPlainText(), "Imported role")
        self.assertEqual(self.page.job_details_count.text(), "2 words")
        self.assertEqual(
            self.store.context_documents(), {"cover_letter": ("letter.txt", "Keep me")}
        )
        self.assertEqual(self.page.detail_rows[0][0].text(), "Example")
        self.assertEqual(self.sender.claude_directory, self.backup.parent)
        self.assertEqual(
            self.page.audio_combos["you"].currentData(), "Imported microphone"
        )
        self.assertEqual(self.store.audio_device("them"), "Keep speakers")
        self.assertEqual(self.page.whisper_combo.currentData(), MODEL)
        self.page._on_whisper_downloaded(MODEL, "")
        self.assertEqual(self.store.whisper_model(), MODEL)
        self.page.open_settings()  # Reopening keeps showing the imported choice
        self.assertEqual(self.page.whisper_combo.currentData(), MODEL)
        self.assertTrue(self.page.capture_checkbox.isChecked())
        self.assertFalse(self.page.tray_checkbox.isChecked())
        self.assertTrue(self.page.click_through_checkbox.isChecked())
        self.assertTrue(self.page.elevation_checkbox.isChecked())
        self.assertIn("next time Que starts", self.panel.status.text())
        self.assertEqual(self.history.path.read_bytes(), original_history)

    def test_presets_are_independent_and_do_not_reset_chat_models(self) -> None:
        """Leave unchecked preset slots and the current chat selection intact."""
        self.page._on_loaded(self.page.revision, ProviderResult("gemini", (FLASH,), ""))
        self.load_models()
        self.sender.set_effort_level("high")
        self.store.save_presets("gemini", [("flash", "low"), ("flash", "low")])
        self.write_backup(
            {
                "presets.gemini.0": {"model": "", "effort": "default"},
                "presets.gemini.1": {"model": "flash", "effort": "high"},
            }
        )
        self.open_backup()
        self.panel.choices["presets.gemini.1"].setChecked(True)
        self.panel.apply_button.click()
        self.assertEqual(
            self.store.presets("gemini")[:2], [("flash", "low"), ("flash", "high")]
        )
        self.assertEqual(self.page.preset_rows[1][1].currentData(), "high")
        self.assertEqual(self.sender.effort_level, "high")

    def test_ai_import_applies_provider_and_preserves_unavailable_saved_defaults(
        self,
    ) -> None:
        """Refresh a restored provider without overwriting its saved model and effort."""
        self.write_backup(
            {"provider": "claude", "claude": {"model": "missing", "effort": "high"}}
        )
        self.open_backup()
        self.panel.select_button.click()
        with patch(
            "core.provider_catalog.ProviderCatalog.refresh", return_value=41
        ) as refresh:
            self.panel.apply_button.click()
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(self.sender.provider, "claude")
        self.assertTrue(self.page.claude_button.isChecked())
        self.page._on_loaded(41, ProviderResult("claude", (FLASH,), "Fixture loaded"))
        self.assertEqual(self.store.defaults("claude"), ("missing", "high"))

        self.load_models()
        self.write_backup({"claude": {"model": "flash", "effort": "high"}})
        self.open_backup()
        self.panel.select_button.click()
        with patch("core.provider_catalog.ProviderCatalog.refresh", return_value=42):
            self.panel.apply_button.click()
        self.assertEqual(self.sender.effort_level, "high")
        self.assertEqual(self.panel.status.text(), "Imported 1 setting.")

    def test_foreign_key_is_skipped_without_discarding_other_settings(self) -> None:
        """Retain the local credential when the backup's key cannot be decrypted."""
        self.store.save("gemini", "flash", "low", KEY)
        self.write_backup({"gemini_key": "aGVsbG8=", "instruction": "Import this"})
        self.open_backup()
        self.assertEqual(set(self.panel.choices), {"instruction"})
        self.assertIn("different Windows account", self.panel.status.text())
        self.panel.select_button.click()
        self.panel.apply_button.click()
        self.assertEqual(self.store.gemini_key(), KEY)
        self.assertEqual(self.store.instruction(), "Import this")

    def test_gemini_settings_import_during_a_claude_chat(self) -> None:
        """Keep the Claude chat active and apply the restored key when Gemini next opens."""
        self.sender.configure("claude", "")
        self.load_models()
        self.sender.set_effort_level("high")
        self.write_backup(
            {
                "gemini": {"model": "flash", "effort": "low"},
                "gemini_key": base64.b64encode(ENCRYPTED).decode("ascii"),
            }
        )
        self.open_backup()
        self.panel.select_button.click()
        with patch("core.provider_catalog.ProviderCatalog.refresh", return_value=42):
            self.panel.apply_button.click()
            self.assertEqual(self.sender.provider, "claude")
            self.assertEqual(self.sender.effort_level, "high")
            self.assertEqual(self.store.gemini_key(), KEY)
            conversation = Conversation("gemini", "A Gemini chat")
            self.history.save(conversation)
            self.window._open_conversation(conversation.conversation_id)
        self.assertEqual(self.sender.provider, "gemini")
        self.assertEqual(self.sender.api_key, KEY)

    def test_new_voice_download_can_replace_the_imported_choice(self) -> None:
        """Keep the standard download action available after restoring a model preference."""
        self.write_backup({"whisper_model": MODEL})
        self.open_backup()
        self.panel.select_button.click()
        with (
            patch.object(self.page.transcriber, "downloaded_models", return_value=[]),
            patch.object(self.page.transcriber, "download") as download,
        ):
            self.panel.apply_button.click()
            self.page._refresh_whisper()  # Whatever this machine's model cache holds
            self.page.download_button.click()
        download.assert_called_once_with(MODEL)
        self.page._on_whisper_downloaded(MODEL, "")
        self.assertEqual(self.store.whisper_model(), MODEL)
        self.assertIn("selected it for transcription", self.page.download_status.text())

    def test_invalid_backups_never_offer_a_review(self) -> None:
        """Handle missing, malformed, unsupported, and invalid backups without changing settings."""
        self.open_backup()
        self.assertIn("Check that the file is readable", self.panel.status.text())
        self.backup.write_text("{", encoding="utf-8")
        self.open_backup()
        self.assertIn("valid Que settings JSON", self.panel.status.text())
        self.backup.write_text("[]", encoding="utf-8")
        self.open_backup()
        self.assertIn("exported by Que", self.panel.status.text())
        self.write_backup({"click_through": True}, version=2)
        self.open_backup()
        self.assertIn("unsupported", self.panel.status.text())
        self.write_backup({"screenshots": ["fixture.png"]})
        self.open_backup()
        self.assertIn("no recognized settings", self.panel.status.text())
        self.write_backup({"click_through": 1, "instruction": "Do not save"})
        self.open_backup()
        self.assertIn(
            "invalid value for Click through window", self.panel.status.text()
        )
        self.assertEqual(self.panel.choices, {})
        self.assertEqual(self.store.data, {})

    def test_cancel_and_matching_backup_leave_preferences_alone(self) -> None:
        """Cancel a preview and identify a backup with no available changes."""
        self.write_backup({"instruction": "New instruction"})
        self.open_backup()
        self.panel.select_button.click()
        with patch(PICKER, return_value=""):
            self.panel.export_button.click()
            self.panel.browse_button.click()
        self.assertTrue(self.panel.apply_button.isEnabled())
        self.panel.cancel_button.click()
        self.assertEqual(self.panel.status.text(), "Import canceled.")
        self.panel._apply()
        self.write_backup({"instruction": ""})
        self.open_backup()
        self.assertIn("already match", self.panel.status.text())
        self.assertEqual(self.store.data, {})

    def test_export_errors_do_not_change_application_files(self) -> None:
        """Keep the live settings and the previous backup safe when exporting fails."""
        self.store.save_instruction("Saved")
        self.page.instruction_input.setPlainText("Saved")
        with patch(PICKER, return_value=str(self.store.path)):
            self.panel.export_button.click()
        self.assertIn("outside Que’s data folder", self.panel.status.text())
        self.backup.write_text("previous backup", encoding="utf-8")
        with (
            patch(PICKER, return_value=str(self.backup)),
            patch("core.settings.os.replace", side_effect=PermissionError),
        ):
            self.panel.export_button.click()
        self.assertIn("writable location", self.panel.status.text())
        self.assertEqual(self.backup.read_text(encoding="utf-8"), "previous backup")
        self.assertEqual(self.store.instruction(), "Saved")

    def test_context_save_failure_does_not_export_or_apply_stale_data(self) -> None:
        """Let the user retry after a Context edit could not be saved."""
        self.page.instruction_input.setPlainText("Pending")
        self.write_backup({"system_tray": False})
        with (
            patch(PICKER, return_value=str(self.backup)),
            patch.object(self.store, "save_instruction", side_effect=OSError),
        ):
            self.panel.export_button.click()
            self.assertIn("Context edits before exporting", self.panel.status.text())
            self.panel.browse_button.click()
            self.assertIn("Context edits before importing", self.panel.status.text())
        self.open_backup()
        self.page.instruction_input.setPlainText("Edited after preview")
        self.panel.select_button.click()
        with patch.object(self.store, "save_instruction", side_effect=OSError):
            self.panel.apply_button.click()
        self.assertTrue(self.store.system_tray())

    def test_file_pickers_use_the_suggested_name_and_handle_cancel(self) -> None:
        """Check the real dialogs' configuration and handle export and import results."""
        dialogs: list[tuple[QFileDialog.AcceptMode, str, bool]] = []

        def complete(dialog: QFileDialog) -> int:
            """Record the configured dialog and supply its modal result.

            Args:
                dialog (QFileDialog): The configured file picker.

            Returns:
                int: The dialog result for export or import.
            """
            mode = dialog.acceptMode()
            selected = dialog.selectedFiles()
            dialogs.append(
                (
                    mode,
                    Path(selected[0]).name if selected else "",
                    dialog.testOption(QFileDialog.Option.DontUseNativeDialog),
                )
            )
            if mode == QFileDialog.AcceptMode.AcceptSave:
                dialog.selectFile(str(self.backup))
                return QFileDialog.DialogCode.Accepted.value
            return QFileDialog.DialogCode.Rejected.value

        self.panel.folder = str(self.backup.parent)
        # The Windows surface probe exercises the actual modal loop. Qt's offscreen
        # backend can crash inside QFileDialog.exec() during the traced regressions.
        with patch.object(QFileDialog, "exec", new=complete):
            self.panel.export_button.click()
            self.assertTrue(self.backup.exists())
            export_status = self.panel.status.text()
            self.panel.browse_button.click()
        self.assertEqual(self.panel.status.text(), export_status)
        self.assertTrue(self.panel.review.isHidden())
        self.assertEqual(
            dialogs,
            [
                (QFileDialog.AcceptMode.AcceptSave, "que-settings.json", True),
                (QFileDialog.AcceptMode.AcceptOpen, "", True),
            ],
        )
