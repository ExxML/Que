"""Run the GitHub Actions CI checks, locally or on GitHub, with the same Python and tools.

The workflows in .github/workflows call this script, so its job list is the single
definition of CI: a local pass means the same commands pass on GitHub. Locally it
runs everything in .venv-ci, built from Python 3.13 (the version CI uses) and the
pinned requirement files, and reinstalls only when one of those files changes.

Usage:
    python test/run_ci.py                    Run every job.
    python test/run_ci.py static regression  Run only the named jobs.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_ci_gate import record_ci_run

REPOSITORY = Path(__file__).resolve().parents[1]
VENV = REPOSITORY / ".venv-ci"
STAMP = VENV / "requirements.sha256"
CI_PYTHON = (3, 13)
ON_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"
PYTHON = "{python}"  # Replaced with the CI interpreter when a step runs.
RUNS = ".stealth-runs"  # Output root; local runs get a fresh folder under it, like CI's clean checkout.

REQUIREMENTS = {
    "static": ["requirements.txt", "test/requirements-ci.txt"],
    "regression": ["requirements.txt", "test/requirements-capture.txt"],
    "compliance": ["requirements.txt"],
}

JOBS: dict[str, list[tuple[str, list[str]]]] = {
    "static": [
        ("Ruff linter", [PYTHON, "-m", "ruff", "check", "src", "test", "native"]),
        (
            "Ruff formatter check",
            [PYTHON, "-m", "ruff", "format", "--check", "src", "test", "native"],
        ),
        (
            "Mypy type checker",
            [PYTHON, "-m", "mypy", "--strict", "src", "test", "native"],
        ),
        (
            "Circular imports",
            [PYTHON, "-m", "pylint", "--disable=all", "--enable=cyclic-import", "src"],
        ),
    ],
    "regression": [
        ("Offline regressions", [PYTHON, "test/stealth/regressions.py"]),
        (
            "Capture receiver JavaScript",
            ["node", "--check", "test/capture/receiver.js"],
        ),
        ("Capture sender JavaScript", ["node", "--check", "test/capture/sender.js"]),
        (
            "Capture recorder transport",
            [
                PYTHON,
                "test/capture/browser_smoke.py",
                "--output",
                ".stealth-runs/capture-transport",
            ],
        ),
        *[
            (
                f"JavaScript syntax {path.name}",
                ["node", "--check", path.relative_to(REPOSITORY).as_posix()],
            )
            for path in sorted((REPOSITORY / "test" / "stealth").glob("*.js"))
        ],
        ("Detector test", ["node", "test/stealth/detector_test.js"]),
        *[
            step
            for browser in ("chrome", "edge")
            for step in (
                (
                    f"{browser} smoke",
                    [
                        PYTHON,
                        "test/stealth/browser_smoke.py",
                        "--browser",
                        browser,
                        "--output",
                        f".stealth-runs/{browser}-smoke",
                    ],
                ),
                (
                    f"{browser} cross-origin-isolated smoke",
                    [
                        PYTHON,
                        "test/stealth/browser_smoke.py",
                        "--browser",
                        browser,
                        "--cross-origin-isolated",
                        "--output",
                        f".stealth-runs/{browser}-isolated-smoke",
                    ],
                ),
                (
                    f"{browser} resource smoke",
                    [
                        PYTHON,
                        "test/stealth/resource_smoke.py",
                        "--browser",
                        browser,
                        "--output",
                        f".stealth-runs/{browser}-resource-smoke",
                    ],
                ),
            )
        ],
    ],
    "compliance": [
        (
            "Offline compliance audit",
            [
                PYTHON,
                "test/run_stealth.py",
                "--mode",
                "offline",
                "--allow-incomplete",
                "--output",
                ".stealth-runs/ci",
            ],
        ),
    ],
}


def main() -> int:
    """Prepare the CI interpreter, run the chosen jobs and print a pass or fail summary.

    Returns:
        int: 0 when every step passed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "jobs",
        nargs="*",
        metavar="job",
        help=f"Jobs to run: {', '.join(JOBS)} (default: all).",
    )
    jobs = parser.parse_args().jobs or list(JOBS)
    if unknown := [job for job in jobs if job not in JOBS]:
        parser.error(f"unknown job {', '.join(unknown)}; choose from {', '.join(JOBS)}")

    python = _prepare_python(
        sorted({path for job in jobs for path in REQUIREMENTS[job]})
    )
    # The audit scripts refuse existing output folders, and .stealth-runs keeps earlier runs' evidence.
    runs = RUNS
    if not ON_GITHUB:
        (REPOSITORY / RUNS).mkdir(exist_ok=True)
        runs = (
            Path(tempfile.mkdtemp(prefix="run-ci-", dir=REPOSITORY / RUNS))
            .relative_to(REPOSITORY)
            .as_posix()
        )
    failures: list[str] = []
    started = time.monotonic()
    started_at = time.time()
    for job in jobs:
        for name, command in JOBS[job]:
            if not _run_step(
                f"{job}: {name}",
                [
                    python
                    if part == PYTHON
                    else part.replace(f"{RUNS}/", f"{runs}/", 1)
                    for part in command
                ],
            ):
                failures.append(f"{job}: {name}")

    print(
        f"\n{'=' * 60}\nCI {'FAILED' if failures else 'PASSED'} in {time.monotonic() - started:.0f}s ({', '.join(jobs)})"
    )
    for failure in failures:
        print(f"  FAILED  {failure}")
    if runs != RUNS:
        if failures:
            print(f"Step outputs kept in {runs}")
        else:
            shutil.rmtree(REPOSITORY / runs, ignore_errors=True)
    if not ON_GITHUB and set(jobs) == set(JOBS):
        record_ci_run(
            started_at, not failures
        )  # Lets agents' stop hooks see that CI ran.
    return 1 if failures else 0


def _prepare_python(requirements: list[str]) -> str:
    """Return an interpreter matching CI with the given requirement files installed.

    Args:
        requirements (list[str]): Requirement file paths relative to the repository.

    Returns:
        str: The path of the interpreter the steps run with.
    """
    if ON_GITHUB:
        _install(sys.executable, requirements)
        return sys.executable

    # One shared venv holds every job's requirements, so switching jobs never reinstalls.
    all_requirements = sorted(
        {path for paths in REQUIREMENTS.values() for path in paths}
    )
    python = VENV / "Scripts" / "python.exe"
    digest = hashlib.sha256(
        b"".join((REPOSITORY / path).read_bytes() for path in all_requirements)
    ).hexdigest()
    if python.exists() and STAMP.exists() and STAMP.read_text() == digest:
        return str(python)

    if not python.exists():
        print(
            f"Creating {VENV.name} with Python {CI_PYTHON[0]}.{CI_PYTHON[1]}...",
            flush=True,
        )
        # shortcut: two agents creating .venv-ci at the same moment can collide; add a lock file if that happens.
        subprocess.run([*_base_python(), "-m", "venv", str(VENV)], check=True)
    _install(str(python), all_requirements)
    STAMP.write_text(digest)
    return str(python)


def _base_python() -> list[str]:
    """Find a Python with CI's major and minor version to build the venv from.

    Returns:
        list[str]: The command that starts that Python.
    """
    if sys.version_info[:2] == CI_PYTHON:
        return [sys.executable]
    version = f"{CI_PYTHON[0]}.{CI_PYTHON[1]}"
    try:
        subprocess.run(
            ["py", f"-{version}", "--version"], check=True, capture_output=True
        )
    except (OSError, subprocess.CalledProcessError):
        sys.exit(
            f"CI uses Python {version}; install it (winget install Python.Python.{version}) and rerun."
        )
    return ["py", f"-{version}"]


def _install(python: str, requirements: list[str]) -> None:
    """Install the requirement files into an interpreter.

    Args:
        python (str): The interpreter to install into.
        requirements (list[str]): Requirement file paths relative to the repository.
    """
    command = [python, "-m", "pip", "install", "--disable-pip-version-check"]
    for path in requirements:
        command += ["-r", path]
    print(f"Installing {', '.join(requirements)}...", flush=True)
    subprocess.run(command, cwd=REPOSITORY, check=True)


def _run_step(name: str, command: list[str]) -> bool:
    """Run one CI step from the repository root, printing its output under a heading.

    Args:
        name (str): The step's display name.
        command (list[str]): The command to run.

    Returns:
        bool: Whether the step exited with status 0.
    """
    print(f"::group::{name}" if ON_GITHUB else f"\n--- {name}", flush=True)
    code = subprocess.run(command, check=False, cwd=REPOSITORY).returncode
    if ON_GITHUB:
        print("::endgroup::")
    if code:
        print(
            f"::error::{name} failed (exit {code})"
            if ON_GITHUB
            else f"--- FAILED: {name} (exit {code})",
            flush=True,
        )
    return code == 0


if __name__ == "__main__":
    sys.exit(main())
