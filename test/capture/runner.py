"""Orchestrate resumable capture sessions and derive results from retained media."""

from __future__ import annotations

import ctypes
import importlib
import json
import secrets
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from stealth.isolation import digest
from stealth.results import Result, Status, atomic_json, write_reports

from capture.adapters import create_adapter
from capture.audio import audio_result
from capture.automation import native_steps
from capture.backdrop import Backdrop
from capture.contracts import APPLICATIONS, DIAGNOSTICS, SURFACES, Cell, Config
from capture.detector import Detector
from capture.diagnostics import focus_result, inventory_result
from capture.media import frames, media_metadata, save_frame
from capture.process import FixtureProcess


class BrowserStage:
    """Keep a standalone fixture window behind Que on the selected real monitor."""

    def __init__(self, directory: Path, url: str, monitor: dict[str, Any]) -> None:
        from run_stealth import browser_path

        browser = browser_path("chrome") or browser_path("edge")
        if browser is None:
            raise RuntimeError("Install Chrome or Edge to display the shared backdrop.")
        left, top, right, bottom = monitor["Monitor"]
        # Park the cursor in the fixture header, outside the measured region and markers.
        self.cursor = (left + (right - left) * 3 // 4, top + (bottom - top) // 20)
        self.process = subprocess.Popen(
            [
                str(browser),
                f"--app={url}",
                "--kiosk",
                "--no-first-run",
                "--autoplay-policy=no-user-gesture-required",
                f"--user-data-dir={directory / 'private' / 'backdrop'}",
                f"--window-position={left},{top}",
                f"--window-size={right - left},{bottom - top}",
            ],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def focus(self) -> None:
        """Bring the real shared fixture forward before the measured action interval."""
        desktop = importlib.import_module("pywinauto").Desktop(backend="uia")
        window = desktop.window(
            title="Que capture audit fixture", process=self.process.pid
        )
        window.wait("visible", timeout=20)
        window.set_focus()
        ctypes.windll.user32.SetCursorPos(*self.cursor)

    def close(self) -> None:
        """Close only the browser instance started for this fixture."""
        if self.process.poll() is None:
            try:
                importlib.import_module("pywinauto").Desktop(backend="uia").window(
                    title="Que capture audit fixture", process=self.process.pid
                ).close()
                self.process.wait(timeout=5)
            finally:
                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=10)


def source_hashes(repository: Path) -> dict[str, str]:
    """Bind reports and resume to the harness implementation."""
    paths = [
        repository / "test" / "run_capture_audit.py",
        *sorted((repository / "test" / "capture").glob("*")),
    ]
    return {
        str(path.relative_to(repository)): digest(path)
        for path in paths
        if path.is_file()
    }


def preflight(config: Config, apps: list[str]) -> list[Result]:
    """Report prerequisites without joining a call or changing application settings."""
    from capture.media import executable

    results = []
    try:
        decoder = executable(config.ffmpeg)
        result = subprocess.run(
            [decoder, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        results.append(
            Result(
                "decoder", "PASS", result.stdout.splitlines()[0], kind="prerequisite"
            )
        )
    except (OSError, ImportError, subprocess.SubprocessError) as error:
        results.append(
            Result("decoder", "INCONCLUSIVE", str(error), kind="prerequisite")
        )
    for app in apps:
        missing = create_adapter(app, config.apps.get(app, {})).probe()
        results.append(
            Result(
                f"setup.{app}",
                "INCONCLUSIVE" if missing else "PASS",
                " ".join(missing)
                if missing
                else "Static prerequisites configured; live authentication and source identity still require measurement.",
                kind="prerequisite",
            )
        )
    if config.target != "source":
        results.append(
            Result(
                "target",
                "UNSUPPORTED",
                "Source fixtures cannot certify a packaged executable. Use the existing packaged audit separately.",
                kind="prerequisite",
            )
        )
    return results


def run_cell(
    root: Path, cell: Cell, config: Config, environment: dict[str, Any]
) -> Path:
    """Acquire one complete source session with immutable evidence and cleanup reporting."""
    directory = root / "attempts" / f"{cell.id}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    nonce = secrets.randbits(32)
    record: dict[str, Any] = {
        "schema": 1,
        "cell": asdict(cell),
        "nonce": nonce,
        "duration": config.duration,
        "state": "started",
        "artifacts": {},
        "actions": {},
        "diagnostics": [],
    }
    atomic_json(directory / "attempt.json", record)
    adapter = create_adapter(cell.app, config.apps.get(cell.app, {}))
    missing = adapter.probe()
    if missing:
        record.update(state="incomplete", error=" ".join(missing))
        atomic_json(directory / "attempt.json", record)
        return directory
    backdrop = Backdrop(nonce)
    fixture: FixtureProcess | None = None
    inspection_fixture: FixtureProcess | None = None
    stage: BrowserStage | None = None
    cleanup_errors = []
    try:
        backdrop.start()
        backdrop.phase(7)
        stage = BrowserStage(
            directory, backdrop.url, environment["monitors"][cell.monitor]
        )
        fixture = FixtureProcess(root)
        adapter.start(cell, directory, backdrop.url)
        settings = config.apps.get(cell.app, {})
        baseline_inventories = {
            name: native_steps(recipe)
            for name, recipe in settings.get("inspect", {}).items()
            if name in ("picker", "shell") and recipe
        }
        stage.focus()
        focus_baseline = fixture.command("state")["foreground"]
        observations = []
        adapter.pump(3)
        # Allow completed setup frames to enter the receiver before freezing calibration.
        for phase in (0, 1, 2, 3, 4, 5, 6):
            backdrop.phase(phase)
            adapter.pump(config.duration + 0.5)
        for index, surface in enumerate(SURFACES, 10):
            if surface == "shutdown":
                continue
            # Pair each visible surface with its intentional-exposure control. Tab/window
            # paths use the in-source controls above because unrelated windows are isolated.
            fixture.command("capture", hidden=True)
            backdrop.phase(index)
            backdrop.wait_presented(cell.source)
            response = fixture.command("surface", surface=surface, monitor=cell.monitor)
            adapter.pump(config.duration + 0.5)
            observations.extend([response, fixture.command("state")])
            record["actions"][str(index)] = response
            fixture.command("hide")
            adapter.pump(0.5)
            if cell.source == "monitor" and surface != "shutdown":
                fixture.command("surface", surface=surface, monitor=cell.monitor)
                adapter.pump(0.5)
                backdrop.phase(index + 100)
                backdrop.wait_presented(cell.source)
                fixture.command("capture", hidden=False)
                adapter.pump(config.duration + 0.5)
                fixture.command("capture", hidden=True)
            backdrop.phase(7)
            fixture.command("hide")
            adapter.pump(0.5)
        operations: dict[str, bool] = {}
        fixture.command("surface", surface="main", monitor=cell.monitor)
        for phase, name in ((70, "share-restart"), (71, "source-change")):
            backdrop.phase(phase)
            adapter.pump(0.3)
            operations[name] = adapter.operate(name)
            adapter.pump(config.duration + 0.5)
        if len(config.monitors) > 1:
            backdrop.phase(72)
            for monitor in config.monitors:
                fixture.command("surface", surface="main", monitor=monitor)
                adapter.pump(config.duration)
            fixture.command("surface", surface="main", monitor=cell.monitor)
            operations["monitor-transition"] = True
        audio_states = []
        for phase in (80, 81, 82):
            backdrop.phase(phase)
            fixture.command("audio", enabled=phase == 81)
            adapter.pump(config.duration + 0.5)
            audio_states.append(fixture.command("state"))
        backdrop.phase(7)
        record["operations"] = operations
        record["audio_states"] = audio_states
        shutdown_phase = 10 + SURFACES.index("shutdown")
        backdrop.phase(shutdown_phase)
        backdrop.wait_presented(cell.source)
        record["actions"][str(shutdown_phase)] = fixture.command(
            "surface", surface="shutdown", monitor=cell.monitor
        )
        fixture.close()
        record["shutdown_exit"] = fixture.process.returncode
        adapter.pump(config.duration + 0.5)
        backdrop.phase(7)
        capture = adapter.finish()
        capture.diagnostics.append(focus_result(focus_baseline, observations))
        # Shell/picker acquisition is outside the pixel interval because opening the
        # chooser itself changes the shared desktop. Every surface is compared.
        settings = config.apps.get(cell.app, {})
        if any(settings.get("inspect", {}).get(name) for name in ("picker", "shell")):
            inspection_fixture = FixtureProcess(root)
        for name in ("picker", "shell"):
            recipe = settings.get("inspect", {}).get(name)
            if not recipe:
                continue
            assert inspection_fixture is not None
            inspection_fixture.command("hide")
            baseline = baseline_inventories.get(name, [])
            inspections: dict[str, Any] = {"baseline": baseline, "surfaces": {}}
            verdicts = []
            for surface in SURFACES:
                if surface == "shutdown":
                    continue
                inspection_fixture.command(
                    "surface", surface=surface, monitor=cell.monitor
                )
                inventory = native_steps(recipe)
                inspections["surfaces"][surface] = inventory
                verdicts.append(
                    inventory_result(
                        name, baseline, inventory, "Que capture audit fixture"
                    )
                )
                inspection_fixture.command("hide")
            atomic_json(directory / f"{name}-comparison.json", inspections)
            worst = next(
                (item for item in verdicts if item.status == "FAIL"),
                next((item for item in verdicts if item.status != "PASS"), verdicts[0]),
            )
            worst.evidence = [f"{name}-comparison.json"]
            capture.diagnostics = [
                item for item in capture.diagnostics if item.check != name
            ] + [worst]
        if capture.path.resolve().parent != directory.resolve():
            raise ValueError("Adapter returned media outside its evidence directory.")
        record.update(
            state="complete",
            media=capture.path.name,
            origin=capture.origin,
            receiver=capture.metadata,
            streams=media_metadata(capture.path, config.ffprobe),
            diagnostics=[asdict(item) for item in capture.diagnostics],
        )
    except Exception as error:  # noqa: BLE001  # Preserve completed evidence across vendor automation failures.
        # URLs and browser exceptions can contain meeting tokens; retain details privately.
        private = directory / "private"
        private.mkdir(exist_ok=True)
        (private / "error.txt").write_text(
            f"{type(error).__name__}: {error}", encoding="utf-8"
        )
        record.update(
            state="incomplete",
            error=f"{type(error).__name__} during acquisition; see the private local error log.",
        )
    finally:
        for resource in (adapter, fixture, inspection_fixture, stage, backdrop):
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:  # noqa: BLE001  # Attempt every owned cleanup and report failures.
                    cleanup_errors.append(
                        f"{type(resource).__name__}: {type(error).__name__}"
                    )
        if fixture is not None:
            atomic_json(directory / "actions.json", fixture.log)
            record["fixture_pid"] = fixture.process.pid
        if inspection_fixture is not None:
            atomic_json(directory / "inspection-actions.json", inspection_fixture.log)
        record["cleanup_errors"] = cleanup_errors
        record["artifacts"] = {
            path.name: digest(path)
            for path in directory.iterdir()
            if path.is_file() and path.name != "attempt.json"
        }
        atomic_json(directory / "attempt.json", record)
    return directory


def assess(root: Path, directory: Path, config: Config) -> list[Result]:
    """Recompute capture verdicts from complete hash-bound media and fixture actions."""
    record = json.loads((directory / "attempt.json").read_text(encoding="utf-8"))
    cell = Cell(**record["cell"])
    relative = directory.relative_to(root).as_posix()
    reasons = []
    for name, expected in record.get("artifacts", {}).items():
        path = directory / name
        if (
            path.resolve().parent != directory.resolve()
            or not path.is_file()
            or digest(path) != expected
        ):
            reasons.append("Missing, altered, or invalid evidence artifact.")
    required = {record.get("media", ""), "actions.json"}
    if not required.issubset(record.get("artifacts", {})):
        reasons.append("Missing media or action evidence hash.")
    if record.get("state") != "complete":
        reasons.append(record.get("error", "Acquisition was interrupted."))
    if record.get("cleanup_errors"):
        reasons.append("Cleanup did not complete.")
    detector = Detector(int(record["nonce"]))
    raw_actions = []
    if "actions.json" in record.get("artifacts", {}) and not reasons:
        raw_actions = json.loads(
            (directory / "actions.json").read_text(encoding="utf-8")
        )
    if not reasons:
        try:
            for frame, timestamp in frames(directory / record["media"], config.ffmpeg):
                detector.feed(frame, timestamp)
        except Exception as error:  # noqa: BLE001  # Corrupt or unsupported media must leave a reportable coverage gap.
            reasons.append(f"Media decoding failed ({type(error).__name__}).")
        reasons.extend(
            detector.health(range(10, 10 + len(SURFACES)), float(record["duration"]))
        )
    receiver = record.get("receiver", {})
    if receiver.get("source_verified") is False:
        reasons.append(
            "The browser did not confirm the requested display-surface type."
        )
    # Delivery gaps are judged per measured phase from decoded timestamps (Detector.health);
    # the receiver's whole-recording maxGap includes the share's startup.
    if (
        receiver.get("errors")
        or receiver.get("ended")
        or receiver.get("muted")
        or receiver.get("skipped")
    ):
        reasons.append(
            "Receiver reported lost, muted, or interrupted frame observations."
        )
    if receiver.get("canvas"):
        reasons.append(
            "Canvas receiver sampling cannot verify every delivered video frame."
        )
    results = []
    for phase, surface in enumerate(SURFACES, 10):
        gaps = list(reasons)
        action = record.get("actions", {}).get(str(phase), {})
        if not any(
            item.get("request", {}).get("surface") == surface
            and item.get("response") == action
            for item in raw_actions
        ):
            gaps.append("Action receipt does not match the original fixture trace.")
        if not action.get("ok") or (
            surface != "shutdown" and not action.get("visible")
        ):
            gaps.append("Missing completed visible fixture action.")
        if action.get("errors"):
            gaps.append("Native window policy reported an error.")
        if surface == "shutdown" and record.get("shutdown_exit") != 0:
            gaps.append(
                "The fixture process did not exit cleanly during the recorded interval."
            )
        if cell.source == "monitor" and surface != "shutdown":
            control = detector.observations.get(phase + 100)
            if control is None or not control.changed or len(control.ticks) < 5:
                gaps.append(
                    "The same surface was not detected with capture protection disabled."
                )
        observed = detector.observations.get(phase)
        # A positive difference is a finding only after the capture path was calibrated.
        status: Status = (
            "INCONCLUSIVE"
            if gaps
            else "FAIL"
            if observed and observed.changed
            else "PASS"
        )
        reason = (
            " ".join(dict.fromkeys(gaps))
            if gaps
            else "Changed or obscured pixels appeared while Que was protected."
            if status == "FAIL"
            else "No exposure detected in calibrated received frames for this configuration."
        )
        evidence = [f"{relative}/attempt.json"]
        if phase in detector.examples:
            name = f"phase-{phase}.png"
            save_frame(directory / name, detector.examples[phase])
            evidence.append(f"{relative}/{name}")
        results.append(
            Result(
                f"{cell.id}.{surface}",
                status,
                reason,
                kind="capture",
                configuration=asdict(cell),
                evidence=evidence,
            )
        )
    atomic_json(directory / "analysis.json", detector.summary())
    by_name = {item["check"]: item for item in record.get("diagnostics", [])}
    if not reasons:
        by_name["audio"] = asdict(
            audio_result(
                directory / record["media"],
                detector,
                record.get("audio_states", []),
                config.ffmpeg,
            )
        )
        for phase, name in (
            (70, "share-restart"),
            (71, "source-change"),
            (72, "monitor-transition"),
        ):
            if record.get("operations", {}).get(name):
                gaps = detector.health([phase], float(record["duration"]))
                observation = detector.observations.get(phase)
                transition_status: Status = (
                    "INCONCLUSIVE"
                    if gaps
                    else "FAIL"
                    if observation and observation.changed
                    else "PASS"
                )
                by_name[name] = asdict(
                    Result(
                        name,
                        transition_status,
                        " ".join(gaps)
                        if gaps
                        else "Calibrated output observed through the configured transition recipe.",
                        kind="capture",
                    )
                )
        local = receiver.get("local_recording")
        if local and local in record.get("artifacts", {}):
            local_detector = Detector(int(record["nonce"]))
            try:
                for frame, timestamp in frames(directory / local, config.ffmpeg):
                    local_detector.feed(frame, timestamp)
                gaps = local_detector.health(
                    range(10, 10 + len(SURFACES)), float(record["duration"])
                )
                changed = any(
                    local_detector.observations.get(phase)
                    and local_detector.observations[phase].changed
                    for phase in range(10, 10 + len(SURFACES))
                )
                local_status: Status = (
                    "INCONCLUSIVE" if gaps else "FAIL" if changed else "PASS"
                )
                by_name["local-recording"] = asdict(
                    Result(
                        "local-recording",
                        local_status,
                        " ".join(gaps)
                        if gaps
                        else "Native recording analyzed independently of receiver footage.",
                        kind="capture",
                        evidence=[local],
                    )
                )
            except (ValueError, OSError, subprocess.SubprocessError):
                by_name["local-recording"] = asdict(
                    Result(
                        "local-recording",
                        "INCONCLUSIVE",
                        "Native recording could not be completely decoded.",
                        kind="capture",
                    )
                )
    for name in DIAGNOSTICS:
        if name == "local-recording" and cell.app not in (
            "zoom-desktop",
            "webex-desktop",
            "obs",
            "snipping-tool",
            "game-bar",
        ):
            continue
        if name == "monitor-transition" and len(config.monitors) == 1:
            continue
        if APPLICATIONS[cell.app][0] == "local" and name in (
            "picker",
            "share-restart",
            "source-change",
        ):
            continue  # Command-line source selection has no picker or meeting share controls.
        if APPLICATIONS[cell.app][0] == "local" and (
            name == "audio" and cell.source != "tab"
        ):
            continue  # Auto-selected screen and window shares carry no audio track.
        if name == "local-recording" and cell.app in (
            "obs",
            "snipping-tool",
            "game-bar",
        ):
            continue  # The recorded media is the primary evidence for these adapters.
        if name in by_name:
            item = by_name[name]
            item["evidence"] = [
                f"{relative}/{value}" for value in item.get("evidence", [])
            ]
            item["check"] = f"{cell.id}.{name}"
            results.append(Result(**item))
        else:
            results.append(
                Result(
                    f"{cell.id}.{name}",
                    "INCONCLUSIVE",
                    f"No complete {name} observation was acquired for this application.",
                    kind="capture",
                    configuration=asdict(cell),
                )
            )
    return results


def report(root: Path, config: Config) -> int:
    """Keep prior failures while letting a completed retry resolve an incomplete attempt."""
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    manifest = [
        Cell(**item)
        for item in json.loads((root / "matrix.json").read_text(encoding="utf-8"))
    ]
    config.monitors = config.monitors or tuple(
        sorted({cell.monitor for cell in manifest})
    )
    if metadata["configuration"] != config.fingerprint():
        raise ValueError("Report configuration differs from the acquired run.")
    latest: dict[str, list[Result]] = {}
    findings = root / "findings.json"
    failures = (
        {
            item["check"]: Result(**item)
            for item in json.loads(findings.read_text(encoding="utf-8"))
        }
        if findings.exists()
        else {}
    )
    for directory in sorted(
        (root / "attempts").glob("*"), key=lambda path: path.stat().st_mtime_ns
    ):
        if not (directory / "attempt.json").is_file():
            continue
        observed = assess(root, directory, config)
        for result in observed:
            if result.status == "FAIL":
                failures[result.check] = result
        cell = Cell(
            **json.loads((directory / "attempt.json").read_text(encoding="utf-8"))[
                "cell"
            ]
        )
        latest[cell.id] = observed
    results = []
    for cell in manifest:
        results.extend(
            latest.get(
                cell.id,
                [
                    Result(
                        cell.id,
                        "INCONCLUSIVE",
                        "This required capture configuration has not run.",
                        kind="capture",
                        configuration=asdict(cell),
                    )
                ],
            )
        )
    by_check = {result.check: result for result in results}
    by_check.update(failures)
    atomic_json(findings, [asdict(result) for result in failures.values()])
    results = list(by_check.values())
    if config.repeats < 3:
        results.append(
            Result(
                "repetitions",
                "INCONCLUSIVE",
                "Development subset: the required three repetitions were not selected.",
                kind="coverage",
            )
        )
    return write_reports(root, metadata, results)
