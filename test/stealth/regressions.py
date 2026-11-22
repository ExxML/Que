"""Run existing unittest regressions with live network connections explicitly denied."""

import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    """Run regression and harness tests while retaining ordinary unittest exit behavior."""
    repository = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repository / "src"), str(repository / "test")]
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import onnxruntime  # type: ignore[import-untyped]  # noqa: F401  # Load before PyQt6, whose older msvcp140.dll crashes onnxruntime

    from stealth.coverage import SourceCoverage

    coverage = SourceCoverage(repository / "src")
    if os.environ.get("QUE_AUDIT_COVERAGE"):
        coverage.start()

    with (
        patch.object(
            socket.socket,
            "connect",
            side_effect=RuntimeError(
                "Live network is disabled for offline regressions."
            ),
        ),
        patch.object(
            socket,
            "create_connection",
            side_effect=RuntimeError(
                "Live network is disabled for offline regressions."
            ),
        ),
    ):
        suite = unittest.defaultTestLoader.discover(
            str(repository / "test"), pattern="test_*.py"
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("QUE_AUDIT_COVERAGE"):
        coverage.stop(Path(os.environ["QUE_AUDIT_COVERAGE"]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
