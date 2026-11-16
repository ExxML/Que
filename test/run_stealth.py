"""Run the separate offline compliance audit or resume guided browser validation."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from stealth.analysis import randomized_schedule
from stealth.coverage import SourceCoverage, coverage_results
from stealth.diagnostics import summarize_native
from stealth.experiments import CONDITIONS, analyze_statistics
from stealth.inventory import compare_inventory, compare_operations, discover_operations
from stealth.isolation import child_environment, digest, stage, supply_gemini_credential
from stealth.manifest import matrix, scenarios, statistical_scenarios
from stealth.results import (
    Result,
    atomic_json,
    read_json,
    verdict,
    write_guide,
    write_reports,
)

REPOSITORY = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).resolve().parent


def offline(directory: Path, metadata: dict[str, Any]) -> list[Result]:
    """Run controlled callback compliance, instantiated inventory, and offline regressions."""
    from stealth.failure_audit import audit_failure_paths
    from stealth.hooks import audit_hooks

    coverage = SourceCoverage(REPOSITORY / "src")
    coverage.start()
    try:
        results, traces = audit_hooks()
        results.extend(audit_failure_paths(directory))
    finally:
        coverage.stop(directory / "native" / "hook-execution.json")
    atomic_json(directory / "native" / "hook-traces.json", traces)
    results.append(
        compare_inventory(REPOSITORY / "src", ASSETS / "stealth" / "inventory.json")
    )
    atomic_json(
        directory / "native" / "operations.json",
        discover_operations(REPOSITORY / "src"),
    )
    results.append(
        compare_operations(REPOSITORY / "src", ASSETS / "stealth" / "operations.json")
    )
    environment = child_environment(directory)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    inventory_path = directory / "native" / "qt-controls.json"
    process = subprocess.run(
        [
            sys.executable,
            str(ASSETS / "stealth" / "qt_inventory.py"),
            str(directory / "isolated"),
            str(inventory_path),
        ],
        cwd=directory,
        check=False,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (directory / "native" / "qt-inventory.log").write_text(
        process.stdout + process.stderr, encoding="utf-8"
    )
    baseline = ASSETS / "stealth" / "qt-controls.json"
    if process.returncode == 0 and inventory_path.exists():
        matched = baseline.exists() and read_json(baseline) == read_json(inventory_path)
        results.append(
            Result(
                "inventory.instantiated",
                "PASS" if matched else "FAIL",
                "Compare synthetic instantiated controls with the reviewed runtime inventory; differences require a coverage decision.",
                evidence=["native/qt-controls.json"],
            )
        )
    else:
        results.append(
            Result(
                "inventory.instantiated",
                "INCONCLUSIVE",
                "Qt inventory could not complete.",
                evidence=["native/qt-inventory.log"],
            )
        )
    environment["QUE_AUDIT_COVERAGE"] = str(
        directory / "native" / "source-execution.json"
    )
    process = subprocess.run(
        [sys.executable, str(ASSETS / "stealth" / "regressions.py")],
        cwd=REPOSITORY,
        check=False,
        env=environment,
        capture_output=True,
        text=True,
        timeout=240,
    )
    (directory / "native" / "regressions.log").write_text(
        process.stdout + process.stderr, encoding="utf-8"
    )
    results.append(
        Result(
            "regressions.offline",
            "PASS" if process.returncode == 0 else "FAIL",
            "Existing regression and harness self-tests with live connections denied.",
            evidence=["native/regressions.log"],
        )
    )
    metadata["scope"] = (
        "Controlled offline audit; no physical browser, live provider, microphone, capture or packaged-release validation."
    )
    results.extend(coverage_results(REPOSITORY / "src", directory))
    atomic_json(directory / "offline-results.json", [asdict(item) for item in results])
    return results


def browser_path(name: str) -> Path | None:
    """Find the installed stable-channel executable without altering existing profiles."""
    suffix = {
        "chrome": "Google/Chrome/Application/chrome.exe",
        "edge": "Microsoft/Edge/Application/msedge.exe",
        "firefox": "Mozilla Firefox/firefox.exe",
    }[name]
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(key)
        if root and (candidate := Path(root) / suffix).is_file():
            return candidate
    return None


def full(directory: Path, metadata: dict[str, Any], base: list[Result]) -> int:
    """Launch normal headed browser profiles and guide real desktop measurements."""
    import win32api  # type: ignore[import-untyped]
    from stealth.native import (
        NativeRecorder,
        audio_environment,
        environment,
        runtime_files,
    )
    from stealth.server import EvidenceStore, ObserverServer

    specifications = scenarios(
        read_json(ASSETS / "stealth" / "inventory.json")
    ) + statistical_scenarios(read_json(directory / "statistics-schedule.json"))
    trials = matrix(
        specifications,
        metadata["browsers"],
        metadata.get("cross_origin_isolated", False),
    )
    store = EvidenceStore(directory, trials)
    server = ObserverServer(
        store,
        ASSETS,
        {"scenarios": [asdict(item) for item in specifications]},
        metadata.get("cross_origin_isolated", False),
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    recorder = NativeRecorder(directory / "native")
    recorder.start()
    children: list[subprocess.Popen[bytes]] = []
    application: subprocess.Popen[bytes] | None = None
    metadata["scope"] = (
        "Guided real desktop audit; no browser-security overrides or synthetic physical input."
    )
    if metadata.get("package_layout") == "onefile":
        base.append(
            Result(
                "packaged.runtime-paths",
                "INCONCLUSIVE",
                "Single-file/custom extraction paths are not verified by this harness. Use the project's standalone Nuitka distribution for complete release validation.",
            )
        )
    metadata["environment"] = environment()
    try:
        metadata["audio_devices"] = audio_environment()
    except OSError as error:
        base.append(
            Result(
                "prerequisite.audio",
                "INCONCLUSIVE",
                f"Audio device enumeration failed: {error}",
            )
        )
    metadata["gemini_credential_available"] = supply_gemini_credential(
        REPOSITORY, directory
    )
    metadata.setdefault("browser_versions", {})
    from core.settings import SettingsStore

    store.redactions = (
        SettingsStore(directory / "isolated" / "data" / "config.json").gemini_key(),
    )
    store.credential_mode = "live"
    if int(sys.getwindowsversion().build) < 22000:
        base.append(
            Result(
                "prerequisite.windows11",
                "INCONCLUSIVE",
                "Required Windows 11 build is unavailable.",
            )
        )
    atomic_json(directory / "metadata.json", metadata)
    try:
        for name in metadata["browsers"]:
            executable = browser_path(name)
            url = f"{server.origin}/?browser={name}#{server.token}"
            if executable is None:
                base.append(
                    Result(
                        f"prerequisite.{name}",
                        "INCONCLUSIVE",
                        "Stable browser executable is unavailable.",
                    )
                )
                continue
            version = win32api.GetFileVersionInfo(str(executable), "\\")
            metadata["browser_versions"][name] = {
                "file_version": [
                    version["FileVersionMS"] >> 16,
                    version["FileVersionMS"] & 65535,
                    version["FileVersionLS"] >> 16,
                    version["FileVersionLS"] & 65535,
                ],
                "sha256": digest(executable),
            }
            profile = directory / "private" / "profiles" / name
            profile.mkdir(parents=True, exist_ok=True)
            flags = (
                ["-no-remote", "-profile", str(profile)]
                if name == "firefox"
                else [f"--user-data-dir={profile}", "--no-first-run", "--new-window"]
            )
            children.append(
                subprocess.Popen(
                    [str(executable), *flags, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        print(
            f"Evidence: {directory}\nUse the browser scenario guide and real input. Live services/audio/downloads are required.\nClaude may retain synthetic conversations in its normal account transcripts.\nCommands: launch, calibration, snapshot, report, stop, quit. Start capture before 'launch' for startup tests."
        )
        while True:
            command = input("audit> ").strip().lower()
            if command == "launch":
                if application is not None and application.poll() is None:
                    print("The isolated application is already running.")
                    continue
                application = subprocess.Popen(
                    metadata["command"],
                    cwd=directory / "fixtures",
                    env=child_environment(directory),
                    # A child inheriting a piped audit> stdin freezes on Windows while the prompt reads it.
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                recorder.pid = application.pid
                children.append(application)
                with (directory / "native" / "lifecycle.jsonl").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(
                        json.dumps(
                            {
                                "type": "launch",
                                "pid": application.pid,
                                "wall_ms": time.time_ns() // 1_000_000,
                            }
                        )
                        + "\n"
                    )
            elif command in ("dummy-key", "live-key"):
                if application is not None and application.poll() is None:
                    print(
                        "Quit the isolated application first, then change the key mode and relaunch."
                    )
                    continue
                if command == "dummy-key":
                    settings = SettingsStore(
                        directory / "isolated" / "data" / "config.json"
                    )
                    settings.save(
                        "gemini", "", "default", "QUE-AUDIT-DUMMY-NOT-A-CREDENTIAL"
                    )
                    store.credential_mode = "dummy"
                else:
                    supply_gemini_credential(REPOSITORY, directory)
                    store.credential_mode = "live"
                print(
                    f"Credential mode: {store.credential_mode}. Use launch to start with these isolated settings."
                )
            elif command == "calibration":
                children.append(
                    subprocess.Popen(
                        [sys.executable, str(ASSETS / "stealth" / "calibration.py")],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            elif command == "snapshot":
                atomic_json(
                    directory / "native" / f"runtime-{time.time_ns()}.json",
                    runtime_files(directory),
                )
                atomic_json(
                    directory / "native" / f"displays-{time.time_ns()}.json",
                    environment(),
                )
                print(
                    "Saved screenshot/model hashes and current display configuration."
                )
            elif command == "report":
                write_reports(
                    directory,
                    metadata,
                    base
                    + store.results()
                    + analyze_statistics(directory)
                    + summarize_native(directory),
                )
                print(f"Updated {directory / 'report.html'}")
            elif command == "stop":
                print(
                    "Quit the isolated Que with its normal Quit control to measure clean shutdown."
                )
            elif command == "quit":
                break
            else:
                print("Commands: launch, calibration, snapshot, report, stop, quit.")
    except (KeyboardInterrupt, EOFError):
        metadata["interrupted"] = True
    finally:
        recorder.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        # Terminate only processes launched by this run, never existing user applications.
        for child in children:
            if child.poll() is None:
                child.terminate()
        store.save()
        atomic_json(directory / "metadata.json", metadata)
    return write_reports(
        directory,
        metadata,
        base
        + store.results()
        + analyze_statistics(directory)
        + summarize_native(directory),
    )


def main() -> int:
    """Create or resume an isolated audit and return 0, 1, 2, or 3 by outcome."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "full"), default="offline")
    parser.add_argument(
        "--target", default="source", help="source or exact packaged .exe path"
    )
    parser.add_argument(
        "--browser", choices=("all", "chrome", "edge", "firefox"), default="all"
    )
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument(
        "--cross-origin-isolated",
        action="store_true",
        help="Serve an additional COOP/COEP condition with normal browser isolation policies.",
    )
    parser.add_argument(
        "--source-audit",
        type=Path,
        help="Source offline run directory to link to a packaged audit",
    )
    args = parser.parse_args()
    if platform.system() != "Windows":
        print(
            "Windows is required; desktop and hook coverage is unsupported on this platform."
        )
        return 2
    sys.path.insert(0, str(REPOSITORY / "src"))
    import ctranslate2  # type: ignore[import-untyped]  # noqa: F401

    directory = (
        args.resume
        or args.output
        or REPOSITORY / ".stealth-runs" / time.strftime("%Y%m%d-%H%M%S")
    ).resolve()
    if args.resume:
        metadata = read_json(directory / "metadata.json")
        base = [
            Result(**item) for item in read_json(directory / "offline-results.json")
        ]
        for relative, expected in metadata.get("harness_hashes", {}).items():
            path = ASSETS / relative
            if not path.exists() or digest(path) != expected:
                raise ValueError(
                    f"Audit harness changed: {relative}; preserve this run and start a new one."
                )
        for relative, expected in metadata["hashes"].items():
            path = directory / "isolated" / relative
            if not path.exists() or digest(path) != expected:
                raise ValueError(
                    f"Staged application changed: {relative}; start a new run."
                )
    else:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "native").mkdir()
        (directory / "browser").mkdir()
        metadata = stage(REPOSITORY, directory, args.target)
        metadata.update(
            mode=args.mode,
            cross_origin_isolated=args.cross_origin_isolated,
            browsers=["chrome", "edge", "firefox"]
            if args.browser == "all"
            else [args.browser],
        )
        from stealth.native import environment

        metadata["environment"] = environment()
        metadata["harness_hashes"] = {
            path.relative_to(ASSETS).as_posix(): digest(path)
            for path in [
                ASSETS / "run_stealth.py",
                ASSETS / "web-detection-test.html",
                ASSETS / "network-origin-test.html",
                *sorted(ASSETS.glob("test_*.py")),
                *sorted((ASSETS / "stealth").glob("*")),
            ]
            if path.is_file()
        }
        atomic_json(directory / "metadata.json", metadata)
        schedule = {
            f"{browser}-{condition}": randomized_schedule(
                74001 + browser_index * 100 + index
            )
            for browser_index, browser in enumerate(metadata["browsers"])
            for index, condition in enumerate(CONDITIONS)
        }
        atomic_json(directory / "statistics-schedule.json", schedule)
        base = (
            offline(directory, metadata)
            if args.target == "source"
            else [
                Result(
                    "packaged.offline",
                    "INCONCLUSIVE",
                    "Run the source offline audit separately; packaged release requires actual desktop evidence.",
                )
            ]
        )
        if args.target != "source" and args.source_audit:
            source_report = read_json(args.source_audit / "report.json")
            if (
                source_report["metadata"]["target"] != "source"
                or source_report["metadata"]["mode"] != "offline"
            ):
                raise ValueError(
                    "--source-audit must reference an offline source report."
                )
            base = [Result(**item) for item in source_report["results"]]
            evidence_root = directory / "source-audit"
            evidence_root.mkdir()
            copied: set[str] = set()
            for result in base:
                retained = []
                for relative in result.evidence:
                    source = (args.source_audit / relative).resolve()
                    if (
                        not source.is_relative_to(args.source_audit.resolve())
                        or not source.is_file()
                    ):
                        raise ValueError(
                            "Linked source audit has missing or out-of-bound evidence."
                        )
                    destination = evidence_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if relative not in copied:
                        shutil.copy2(source, destination)
                        if digest(source) != digest(destination):
                            raise ValueError(
                                "Linked source evidence changed while copying."
                            )
                        copied.add(relative)
                    retained.append(f"source-audit/{relative}")
                result.evidence = retained
            metadata["source_audit"] = {
                "report_sha256": digest(args.source_audit / "report.json"),
                "revision": source_report["metadata"]["revision"],
                "hashes": source_report["metadata"]["hashes"],
            }
        atomic_json(directory / "offline-results.json", [asdict(item) for item in base])
        atomic_json(directory / "metadata.json", metadata)
    write_guide(
        directory,
        [
            asdict(item)
            for item in scenarios(read_json(ASSETS / "stealth" / "inventory.json"))
        ],
    )
    if metadata["mode"] == "full":
        if args.report_only:
            from stealth.server import EvidenceStore

            store = EvidenceStore(
                directory,
                matrix(
                    scenarios(read_json(ASSETS / "stealth" / "inventory.json"))
                    + statistical_scenarios(
                        read_json(directory / "statistics-schedule.json")
                    ),
                    metadata["browsers"],
                    metadata.get("cross_origin_isolated", False),
                ),
            )
            return write_reports(
                directory,
                metadata,
                base
                + store.results()
                + analyze_statistics(directory)
                + summarize_native(directory),
            )
        return full(directory, metadata, base)
    code = write_reports(directory, metadata, base)
    counts = {
        status: sum(result.status == status for result in base)
        for status in ("PASS", "FAIL", "INCONCLUSIVE", "UNSUPPORTED")
    }
    print(
        f"Offline audit {verdict(base)[0]}: {counts}\nReport: {directory / 'report.html'}"
    )
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(
            f"Audit could not complete: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        raise SystemExit(3) from error
