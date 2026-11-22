import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.settings import CONFIG_GROUPS, SettingsStore
from ui.directory_picker import choose_file

CONFIG_FILTER = "Que settings (*.json)"


class ConfigPanel(QWidget):
    """Export saved settings and review individual changes before importing."""

    imported = pyqtSignal(object)

    def __init__(
        self, settings_store: SettingsStore, flush_edits: Callable[[], bool]
    ) -> None:
        super().__init__()
        self.settings_store = settings_store
        self.flush_edits = flush_edits
        self.pending: dict[str, Any] = {}
        self.choices: dict[str, QCheckBox] = {}
        self.folder = str(Path.home())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        description = QLabel(
            "Back up your saved settings or restore selected settings from a backup. "
            "Chats, transcripts, screenshots, and downloaded models stay on this device."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        privacy = QLabel(
            "The backup includes your Context document text and local paths. "
            "Your Gemini API key stays encrypted for this Windows account."
        )
        privacy.setWordWrap(True)
        layout.addWidget(privacy)

        actions = QHBoxLayout()
        self.export_button = QPushButton("Export settings…")
        self.browse_button = QPushButton("Import settings…")
        self.export_button.clicked.connect(self._export)
        self.browse_button.clicked.connect(self._browse)
        actions.addWidget(self.export_button)
        actions.addWidget(self.browse_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.status = QLabel()
        self.status.setAccessibleName("Settings backup status")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)

        self.review = QWidget()
        review_layout = QVBoxLayout(self.review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("Choose which settings to replace with values from this backup.")
        note.setWordWrap(True)
        review_layout.addWidget(note)
        review_actions = QHBoxLayout()
        self.apply_button = QPushButton("Import selected")
        self.cancel_button = QPushButton("Cancel")
        self.select_button = QPushButton("Select all")
        self.apply_button.clicked.connect(self._apply)
        self.cancel_button.clicked.connect(self._cancel)
        self.select_button.clicked.connect(self._toggle_selection)
        review_actions.addWidget(self.apply_button)
        review_actions.addWidget(self.select_button)
        review_actions.addWidget(self.cancel_button)
        review_actions.addStretch()
        review_layout.addLayout(review_actions)
        self.choice_layout = QVBoxLayout()
        self.choice_layout.setSpacing(6)
        review_layout.addLayout(self.choice_layout)
        layout.addWidget(self.review)
        layout.addStretch()
        self.review.hide()

    def _export(self) -> None:
        """Ask for a destination and export all saved preferences in one operation."""
        path = choose_file(
            self,
            "Export Que settings",
            self.folder,
            CONFIG_FILTER,
            save_as="que-settings.json",
        )
        if not path:
            return
        if not self.flush_edits():
            self._set_status("Save your Context edits before exporting settings.")
            return
        target = Path(path)
        try:
            self.settings_store.export_config(target)
        except ValueError as error:
            self._set_status(str(error))
        except OSError:
            self._set_status(
                "Could not export settings. Choose a writable location and try again."
            )
        else:
            self.folder = str(target.parent)
            self._set_status(f"Settings exported to {target.name}.")

    def _browse(self) -> None:
        """Open a backup and show the preferences that differ from current values."""
        path = choose_file(self, "Import Que settings", self.folder, CONFIG_FILTER)
        if not path:
            return
        self._clear_review()
        if not self.flush_edits():
            self._set_status("Save your Context edits before importing settings.")
            return
        source = Path(path)
        try:
            values, key_unavailable = self.settings_store.read_config(source)
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            self._set_status(
                "Could not read the backup. Choose a valid Que settings JSON file."
            )
            return
        except ValueError as error:
            self._set_status(str(error))
            return
        except OSError:
            self._set_status(
                "Could not open the backup. Check that the file is readable and try again."
            )
            return
        self.folder = str(source.parent)
        current = self.settings_store.config_values()
        self.pending = {
            key: value for key, value in values.items() if value != current[key]
        }
        key_note = (
            " The Gemini key belongs to a different Windows account or cannot be opened."
            if key_unavailable
            else ""
        )
        if not self.pending:
            self._set_status(
                f"Your settings already match the available settings in {source.name}.{key_note}"
            )
            return
        self._set_status(f"Review changes from {source.name}.{key_note}")
        self._show_review()

    def _show_review(self) -> None:
        """Group changed settings by tab and leave each change unselected."""
        for section, fields in CONFIG_GROUPS.items():
            available = self.pending.keys() & fields.keys()
            if not available:
                continue
            heading = QLabel(section)
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            self.choice_layout.addWidget(heading)
            for key, label in fields.items():
                if key not in available:
                    continue
                value = self.pending[key]
                if isinstance(value, bool):
                    label = f"{label} · {'On' if value else 'Off'}"
                checkbox = QCheckBox(label)
                checkbox.setAccessibleName(f"Import {label}")
                checkbox.toggled.connect(self._update_selection)
                self.choice_layout.addWidget(checkbox)
                self.choices[key] = checkbox
        self._update_selection()
        self.review.show()

    def _update_selection(self) -> None:
        """Enable import only when at least one setting is selected."""
        count = sum(box.isChecked() for box in self.choices.values())
        self.apply_button.setEnabled(count > 0)
        self.select_button.setText(
            "Clear selection" if count == len(self.choices) else "Select all"
        )

    def _toggle_selection(self) -> None:
        """Select or clear every setting currently available to import."""
        checked = not all(box.isChecked() for box in self.choices.values())
        for box in self.choices.values():
            box.setChecked(checked)

    def _apply(self) -> None:
        """Commit the checked values, then notify the settings page to apply them."""
        selected = {
            key: self.pending[key]
            for key, box in self.choices.items()
            if box.isChecked()
        }
        if not selected:
            return
        if not self.flush_edits():
            self._set_status("Save your Context edits before importing settings.")
            return
        try:
            self.settings_store.import_config(selected)
        except OSError:
            self._set_status(
                "Could not save imported settings. Check that Que’s data folder is writable and try again."
            )
            return
        self._clear_review()
        count = len(selected)
        result = "Imported 1 setting." if count == 1 else f"Imported {count} settings."
        if "elevate_helper" in selected:
            result += " The admin shortcuts setting applies the next time Que starts."
        self._set_status(result)
        self.imported.emit(set(selected))

    def _cancel(self) -> None:
        """Discard the preview when the user cancels."""
        self._clear_review()
        self._set_status("Import canceled.")

    def _clear_review(self) -> None:
        """Remove the previous backup's selectable settings."""
        self.review.hide()
        self.pending.clear()
        self.choices.clear()
        while (item := self.choice_layout.takeAt(0)) is not None:
            widget = item.widget()
            assert widget is not None
            widget.hide()
            widget.deleteLater()

    def _set_status(self, message: str) -> None:
        """Show an operation result inline using plain text."""
        self.status.setText(message)
        self.status.show()
