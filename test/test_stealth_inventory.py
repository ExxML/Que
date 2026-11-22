"""Verify signal inventory comparisons across Python formatting differences."""

import tempfile
import unittest
from pathlib import Path

from stealth.inventory import compare_inventory
from stealth.results import atomic_json


class ConnectionInventoryTests(unittest.TestCase):
    """Preserve connection drift detection across Python versions."""

    def setUp(self) -> None:
        """Create a signal connection with the tuple unpacking used by resume settings."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "src"
        (self.source / "ui").mkdir(parents=True)
        self.expression = (
            "self.button.clicked.connect(lambda: self.save("
            "[detail for position, detail in enumerate(self.details()) if position != 0]))"
        )
        (self.source / "ui" / "view.py").write_text(
            f"class View:\n    def build(self):\n        {self.expression}\n",
            encoding="utf-8",
        )
        self.prefix = "ui/view.py:View.build/"
        self.baseline = root / "inventory.json"

    def test_tuple_target_parentheses_are_equivalent(self) -> None:
        """Accept the reviewed connection with or without parentheses around the tuple target."""
        for target in ("position, detail", "(position, detail)"):
            with self.subTest(target=target):
                expression = self.expression.replace("position, detail", target)
                atomic_json(
                    self.baseline,
                    {"controls": {}, "connections": [self.prefix + expression]},
                )
                self.assertEqual(
                    compare_inventory(self.source, self.baseline).status, "PASS"
                )

    def test_connection_changes_still_fail(self) -> None:
        """Reject changed handlers, filters, tuple order, locations and removed connections."""
        original = self.prefix + self.expression
        changed = (
            original.replace("self.save", "self.remove"),
            original.replace("!= 0", "== 0"),
            original.replace("position, detail", "detail, position"),
            original.replace("ui/view.py", "ui/other.py"),
            original.replace("View.build", "View.update"),
        )
        for connections in ([], *([item] for item in changed), [original, changed[0]]):
            with self.subTest(connections=connections):
                atomic_json(self.baseline, {"controls": {}, "connections": connections})
                result = compare_inventory(self.source, self.baseline)
                self.assertEqual(result.status, "FAIL")
                self.assertIn("connections", result.reason)


if __name__ == "__main__":
    unittest.main()
