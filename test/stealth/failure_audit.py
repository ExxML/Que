"""Keep optimistic native failure behavior visible as a genuine compliance finding."""

from pathlib import Path
from unittest.mock import MagicMock

from stealth.results import Result, atomic_json
from ui.window.main_window import MainWindow


def audit_failure_paths(directory: Path) -> list[Result]:
    """Reproduce a topmost-query error without claiming a physical occlusion occurred."""
    window = MagicMock()
    window.frameGeometry.side_effect = RuntimeError("synthetic geometry-query failure")
    reported_topmost = MainWindow._is_topmost_window(window)
    atomic_json(
        directory / "native" / "failure-traces.json",
        {
            "check": "failure.topmost-query-error",
            "trigger": "Raise RuntimeError from frameGeometry inside the real _is_topmost_window method.",
            "reported_topmost": reported_topmost,
            "expected": "A failed observation must not assert verified topmost state.",
            "scope": "Controlled native failure handling; physical visibility and website detection remain unmeasured.",
        },
    )
    return [
        Result(
            "failure.topmost-query-error",
            "FAIL" if reported_topmost else "PASS",
            "Topmost query returns True after a geometry-query exception; native diagnostic success is unverified.",
            evidence=["native/failure-traces.json"],
        )
    ]
