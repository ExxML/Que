"""Tests for the stop gate that makes agents run CI before finishing."""

import unittest

from agent_ci_gate import MAX_BLOCKS, stop_reason


class StopReasonTests(unittest.TestCase):
    """Check when an agent may finish its turn."""

    def test_agent_without_changes_may_finish(self) -> None:
        """Let an agent that changed nothing finish without CI."""
        self.assertIsNone(stop_reason(None, None, 0, None))

    def test_change_without_later_run_blocks(self) -> None:
        """Block an agent whose last change came after the last CI run started."""
        self.assertIn(
            "have not run",
            stop_reason(10.0, {"started": 5.0, "passed": True}, 0, None) or "",
        )
        self.assertIn("have not run", stop_reason(10.0, None, 0, None) or "")

    def test_passing_run_after_change_allows_finish(self) -> None:
        """Let an agent finish once a passing run started after its change."""
        self.assertIsNone(stop_reason(10.0, {"started": 11.0, "passed": True}, 0, None))

    def test_failed_run_blocks_once(self) -> None:
        """Ask once about a failed run, then accept the agent's explanation."""
        run = {"started": 11.0, "passed": False}
        self.assertIn("failed", stop_reason(10.0, run, 0, None) or "")
        self.assertIsNone(stop_reason(10.0, run, 1, 11.0))

    def test_repeated_blocks_give_up(self) -> None:
        """Stop blocking after the limit so a broken setup cannot trap an agent."""
        self.assertIsNone(stop_reason(10.0, None, MAX_BLOCKS, None))


if __name__ == "__main__":
    unittest.main()
