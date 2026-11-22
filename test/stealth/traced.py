"""Run a script with Que source tracing, so subprocess fixtures count as offline execution evidence.

    python test/stealth/traced.py SCRIPT [ARGS ...]

Without QUE_AUDIT_COVERAGE the script just runs. With it, executed Que lines are written next to
that file as source-execution-<pid>.json, which the compliance coverage check merges.
"""

import os
import runpy
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPOSITORY / "test")]

from stealth.coverage import SourceCoverage


def main() -> int:
    """Run the script named on the command line, tracing Que source when coverage is requested.

    Returns:
        int: The script's exit status.
    """
    script, sys.argv = sys.argv[1], sys.argv[1:]
    target = os.environ.get("QUE_AUDIT_COVERAGE")
    coverage = SourceCoverage(REPOSITORY / "src")
    if target:
        coverage.start()
    code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1 if exit_.code else 0
    finally:
        if target:
            coverage.stop(
                Path(target).with_name(f"source-execution-{os.getpid()}.json")
            )
    return code


if __name__ == "__main__":
    sys.exit(main())
