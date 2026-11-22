"""Verify signal inventory comparisons across Python formatting differences."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from stealth import approve
from stealth.coverage import gap_key, ratchet_result
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


class ExecutionGapRatchetTests(unittest.TestCase):
    """Check that the known execution gaps can only shrink."""

    def test_new_and_covered_gaps_fail(self) -> None:
        """Fail on new unexecuted code and on known gaps that tests now execute."""
        gap = {"source": "a.py", "kind": "If", "trigger": "if x:"}
        known = [gap_key(gap)]
        self.assertEqual(ratchet_result([gap], known).status, "PASS")
        self.assertEqual(
            ratchet_result([gap, {**gap, "trigger": "if y:"}], known).status, "FAIL"
        )
        self.assertEqual(ratchet_result([], known).status, "FAIL")


class OperationApprovalTests(unittest.TestCase):
    """Approve only reviewed operation changes, and code that merely moved lines."""

    def test_moved_code_is_approved_but_real_changes_are_not(self) -> None:
        """Let --moved approve shifted operations while a real change stays unapproved."""
        moved = {
            "category": "routine",
            "source": "a.py",
            "expression": "def f():",
            "sha256": "1",
        }
        changed = {
            "category": "branch",
            "source": "a.py",
            "expression": "if x:",
            "sha256": "2",
        }
        baseline = {
            "a.py:1:routine": {**moved, "line": 1},
            "a.py:5:branch": {**changed, "line": 5},
        }
        current = {
            "a.py:3:routine": {**moved, "line": 3},
            "a.py:7:branch": {**changed, "line": 7, "sha256": "3"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operations.json"
            atomic_json(path, baseline)
            with (
                patch.object(approve, "APPROVED", path),
                patch.object(approve, "discover_operations", return_value=current),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(approve._approve_operations([], moved=True), 0)
                result = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(result["a.py:3:routine"], current["a.py:3:routine"])
                self.assertNotIn("a.py:1:routine", result)
                self.assertNotIn(
                    "a.py:7:branch", result
                )  # A real change still needs review
                self.assertEqual(
                    approve._approve_operations(["a.py:7:branch", "a.py:5:branch"]), 0
                )
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), current)
