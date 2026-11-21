"""Check that protection warnings show, hide, and resolve in the status banner."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QWidget
from qt_application import application

from core.ai_sender import AISender
from core.hook_client import HookClient
from core.screenshot_manager import ScreenshotManager
from core.settings import SettingsStore
from ui.window.main_window import MainWindow
from ui.window.status_banner import MAX_ENTRIES, StatusBanner


class StatusBannerTests(unittest.TestCase):
    """Keep warnings out of the chat while never losing one that still holds."""

    def setUp(self) -> None:
        """Create a banner inside a plain window."""
        self.app = application()
        self.window = QWidget()
        self.banner = StatusBanner(self.window)

    def test_repeated_warning_is_one_row(self) -> None:
        """Merge a warning reported again instead of listing it twice."""
        self.banner.show_warning("Reduced.")
        self.banner.show_warning("Reduced.", critical=True)
        self.assertEqual(list(self.banner.entries), ["Reduced."])
        self.assertTrue(self.banner.entries["Reduced."].critical)
        self.assertEqual(self.banner.rows.count(), 1)

    def test_dismissed_condition_leaves_the_badge(self) -> None:
        """Hide a lasting condition behind the badge until it is shown again or resolved."""
        self.banner.show_warning("Not elevated.")
        self.assertTrue(self.banner.wanted())
        self.assertTrue(self.banner.badge.isHidden())
        self.banner._dismiss("Not elevated.")
        self.assertFalse(self.banner.wanted())
        self.assertFalse(self.banner.badge.isHidden())
        self.banner.show_warning("Not elevated.")  # Reported again: stays hidden
        self.assertFalse(self.banner.wanted())
        self.banner.show_all()
        self.assertTrue(self.banner.wanted())
        self.banner.clear_warning("Not elevated.")
        self.assertEqual(self.banner.entries, {})
        self.assertTrue(self.banner.badge.isHidden())

    def test_dismissed_event_is_forgotten(self) -> None:
        """Forget a one-off leak once dismissed, leaving no badge behind."""
        self.banner.show_warning("Leaked.", critical=True, lasting=False)
        self.banner._dismiss("Leaked.")
        self.assertEqual(self.banner.entries, {})
        self.assertTrue(self.banner.badge.isHidden())

    def test_event_flood_drops_oldest_events_only(self) -> None:
        """Bound the rows by dropping old leaks, never a condition that still holds."""
        self.banner.show_warning("Condition.")
        for index in range(MAX_ENTRIES + 3):
            self.banner.show_warning(f"Leak {index}.", critical=True, lasting=False)
        self.assertEqual(len(self.banner.entries), MAX_ENTRIES)
        self.assertIn("Condition.", self.banner.entries)
        self.assertIn(f"Leak {MAX_ENTRIES + 2}.", self.banner.entries)

    def test_height_follows_wrapped_text(self) -> None:
        """Report a taller banner for narrower widths and none when nothing shows."""
        self.banner.show_warning("A long warning that wraps onto several lines. " * 4)
        self.assertGreater(
            self.banner.heightForWidth(200), self.banner.heightForWidth(600)
        )


class MainWindowBannerTests(unittest.TestCase):
    """Dock the banner below the title bar without shrinking the chat."""

    def setUp(self) -> None:
        """Create a main window with an isolated settings store and no model discovery."""
        self.app = application()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SettingsStore(Path(directory.name) / "config.json")
        with patch("core.provider_catalog.ProviderCatalog.refresh", return_value=1):
            self.window = MainWindow(AISender(store), ScreenshotManager())
        self.addCleanup(self.window.close)
        self.window.visibility_timer.stop()
        self.app.processEvents()
        self.banner = self.window.status_banner

    def test_window_grows_and_shrinks_with_the_banner(self) -> None:
        """Grow the window by the banner's height, keep the chat height, and give it back."""
        height = self.window.height()
        pages = self.window.pages.height()
        self.banner.show_warning("Reduced protection.")
        self.app.processEvents()
        self.assertGreater(self.window.status_banner_height, 0)
        self.assertEqual(
            self.window.height(), height + self.window.status_banner_height
        )
        self.assertEqual(self.window.pages.height(), pages)
        self.assertGreater(self.window.pages.y(), self.banner.y())
        self.banner.clear_warning("Reduced protection.")
        self.app.processEvents()
        self.assertEqual(self.window.height(), height)
        self.assertTrue(self.banner.isHidden())

    def test_rows_resize_at_once_through_dismiss_and_badge(self) -> None:
        """Fit the rows shown after each change before the event loop runs."""
        height = self.window.height()
        self.banner.show_warning("Not elevated.")
        one_row = self.window.status_banner_height
        self.banner._dismiss("Not elevated.")
        self.banner.show_warning("Leaked.", critical=True, lasting=False)
        self.assertEqual(self.window.status_banner_height, one_row)
        self.banner.show_all()
        self.assertGreater(self.window.status_banner_height, one_row)
        self.assertFalse(self.banner.isHidden())
        self.banner._dismiss("Leaked.")
        self.assertEqual(self.window.status_banner_height, one_row)
        self.assertFalse(self.banner.isHidden())
        self.banner._dismiss("Not elevated.")
        self.assertEqual(self.window.height(), height)
        self.assertTrue(self.banner.isHidden())


class HookClientWarningTests(unittest.TestCase):
    """Separate reduced protection, outages, and leaks, and resolve outages on reconnect."""

    def test_outage_resolves_when_a_helper_connects(self) -> None:
        """Resolve a critical helper error once a helper reports ready."""
        application()
        client = HookClient()
        alerts: list[str] = []
        failures: list[str] = []
        leaks: list[str] = []
        resolved: list[str] = []
        client.alert.connect(alerts.append)
        client.failed.connect(failures.append)
        client.leaked.connect(leaks.append)
        client.resolved.connect(resolved.append)
        client.dispatch({"type": "error", "text": "Slow fallback."})
        client.dispatch({"type": "error", "text": "No hook.", "critical": True})
        client.dispatch({"type": "leak", "events": ["A"], "restored": True})
        self.assertEqual(failures, ["Slow fallback."])
        self.assertEqual(alerts, ["No hook."])
        self.assertEqual(len(leaks), 1)
        client.dispatch({"type": "ready"})
        self.assertEqual(resolved, ["No hook."])
        client.dispatch({"type": "ready"})
        self.assertEqual(resolved, ["No hook."])


if __name__ == "__main__":
    unittest.main()
