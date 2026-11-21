"""Check effort selection and shared popup behavior through Qt mouse events."""

import unittest
from collections.abc import Callable
from typing import cast
from unittest.mock import MagicMock

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.ai_sender import AISender
from core.model_catalog import ModelOption
from ui.input.input_field import InputField
from ui.input.selection_dropdown import SelectionDropdown

# PyQt6's stubs type QTest's C++ namespace functions as instance methods.
mouse_click = cast(Callable[[QWidget, Qt.MouseButton], None], QTest.mouseClick)
key_click = cast(Callable[[QWidget, Qt.Key], None], QTest.keyClick)

TEST_MODELS = (
    ModelOption(
        "gemini-3-flash-preview",
        "Gemini 3 Flash Preview",
        ("minimal", "low", "medium", "high"),
    ),
    ModelOption("gemini-2.5-flash", "Gemini 2.5 Flash", ("low", "medium", "high")),
)


class EffortSelectorTests(unittest.TestCase):
    """Exercise the settings UI and its connection to captured API requests."""

    def setUp(self) -> None:
        """Create a real settings row with a disconnected Gemini client."""
        self.app = application()
        self.window = QWidget()
        self.window.resize(600, 350)
        self.field = InputField(self.window)
        self.field.setGeometry(0, 200, 600, 150)
        self.settings = self.field.input_settings
        settings = MagicMock(
            **{"gemini_key.return_value": "", "provider.return_value": "gemini"}
        )
        self.sender = AISender(settings, client=MagicMock())
        self.sender.set_models(TEST_MODELS)
        self.field.model_changed.connect(self.sender.set_model)
        self.field.effort_changed.connect(self.sender.set_effort_level)
        self.settings.set_models(TEST_MODELS)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)
        self.addCleanup(self.settings.close_popups)

    def test_selection_and_model_fallback(self) -> None:
        """Keep the UI and backend effort synchronized across model switches."""
        self.assertEqual(self.settings.effort_dropdown.current_display, "Minimal")
        self._select(self.settings.model_dropdown, "gemini-2.5-flash")
        self.assertEqual(self.sender.effort_level, "low")
        self.assertEqual(self.settings.effort_dropdown.current_display, "Low")
        self.assertNotIn(
            "minimal", [value for _, value in self.settings.effort_dropdown.options]
        )
        self._select(self.settings.effort_dropdown, "medium")
        captured = self.sender.prepare_request("old")
        self._select(self.settings.model_dropdown, "gemini-3-flash-preview")
        self.assertEqual(self.sender.effort_level, "medium")
        self._select(self.settings.effort_dropdown, "high")
        self.assertEqual(captured.effort_level, "medium")
        self.assertEqual(self.sender.prepare_request("new").effort_level, "high")

    def test_popup_dismissal_and_reposition(self) -> None:
        """Close on Escape, outside click, and send while following input resize."""
        dropdown = self.settings.effort_dropdown
        mouse_click(dropdown, Qt.MouseButton.LeftButton)
        assert dropdown.popup is not None
        before = dropdown.popup.y()
        self.settings.reposition_popups(15)
        self.assertEqual(dropdown.popup.y(), before + 15)
        key_click(self.field.input_field, Qt.Key.Key_Escape)
        self.assertIsNone(dropdown.popup)
        mouse_click(dropdown, Qt.MouseButton.LeftButton)
        mouse_click(self.window, Qt.MouseButton.LeftButton)
        self.assertIsNone(dropdown.popup)
        mouse_click(dropdown, Qt.MouseButton.LeftButton)
        mouse_click(self.settings.model_dropdown, Qt.MouseButton.LeftButton)
        self.assertIsNone(dropdown.popup)
        self.assertIsNotNone(self.settings.model_dropdown.popup)
        self.field.input_field.setPlainText("hello")
        key_click(self.field.input_field, Qt.Key.Key_Return)
        self.assertIsNone(self.settings.model_dropdown.popup)
        self.assertIsNone(dropdown.popup)

    def _select(self, selection_dropdown: SelectionDropdown, value: str) -> None:
        """Choose an option by clicking its popup row.

        Args:
            selection_dropdown (SelectionDropdown): The selector to operate.
            value (str): The desired option value.
        """
        mouse_click(selection_dropdown, Qt.MouseButton.LeftButton)
        popup = selection_dropdown.popup
        assert popup is not None
        items = popup.scroll_area.widget()
        assert items is not None
        layout = items.layout()
        assert layout is not None
        index = [option for _, option in selection_dropdown.options].index(value)
        item = layout.itemAt(index)
        assert item is not None
        widget = item.widget()
        assert widget is not None
        popup.scroll_area.ensureWidgetVisible(widget)
        mouse_click(widget, Qt.MouseButton.LeftButton)
        self.assertIsNone(selection_dropdown.popup)


if __name__ == "__main__":
    unittest.main()
