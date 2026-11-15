"""Validate bounded action receipts against retained artifacts, never free-text claims."""

import hashlib
import json
from pathlib import Path
from typing import Any

EVENT_PROOF = {
    "browser_audio": {"audio-sample"},
    "frames": {"capture-frame"},
    "baseline": {"capture-baseline"},
    "positive_control": {"capture-positive"},
    "capture_source": {"capture-track"},
    "idle_probe": {"idle-state"},
    "pressure_probe": {"pressure-sample"},
    "eyedropper_probe": {"eyedropper-result"},
    "screen_probe": {"screen-details"},
    "worklet_probe": {"audio-quantum"},
    "gpu_probe": {"gpu-sample"},
    "storage_probe": {"storage-sample"},
    "memory_probe": {"memory-sample"},
    "service_worker_probe": {"service-worker"},
    "resource_timing_probe": {"resource"},
    "owned_endpoint_probe": {"owned-endpoint"},
    "clipboard_probe": {"clipboard-read"},
    "directory_probe": {"directory-read"},
    "camera_probe": {"saved-frame"},
}


def artifact_path(directory: Path, relative: str) -> Path:
    """Resolve an evidence path inside this run, rejecting links outside its boundary."""
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError("Evidence must be a retained file inside this run.")
    return path


def validate_reference(
    reference: str,
    role: str,
    trial: dict[str, Any],
    events: list[dict[str, Any]],
    directory: Path,
) -> list[str]:
    """Check a typed event reference or a hash-bound receipt and its raw artifacts.

    Args:
        reference (str): Event sequence or relative receipt path with SHA-256.
        role (str): Required evidence role.
        trial (dict[str, Any]): Exact attempted action and configuration.
        events (list[dict[str, Any]]): Session events, including calibration setup.
        directory (Path): Private run directory.

    Returns:
        list[str]: Retained supporting paths for report links.
    """
    if reference.startswith("event:"):
        sequence = int(reference.removeprefix("event:"))
        event = next(item for item in events if item["seq"] == sequence)
        if event["type"] not in EVENT_PROOF.get(role, set()):
            raise ValueError(f"Event cannot establish {role}.")
        if role not in {"baseline", "positive_control", "capture_source"} and (
            event.get("attempt") != trial["attempt"]
            or not trial["start_ms"] <= event["wall_ms"] <= trial["end_ms"]
        ):
            raise ValueError("Evidence is outside this action interval.")
        if role == "browser_audio" and event.get("data", {}).get("state") != "running":
            raise ValueError("Audio context was not running.")
        if role == "camera_probe" and event.get("data", {}).get("kind") != "camera":
            raise ValueError(
                "A display capture cannot substitute for a physical camera frame."
            )
        return []
    if not reference.startswith("receipt:"):
        raise ValueError(
            "Free text is not action evidence; use receipt:path#sha256 or event:sequence."
        )
    relative, expected = reference.removeprefix("receipt:").rsplit("#", 1)
    path = artifact_path(directory, relative)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("Receipt hash differs from the reviewed evidence.")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (
        receipt.get("schema") != 1
        or receipt.get("attempt") != trial["attempt"]
        or receipt.get("trial") != trial["id"]
        or role not in receipt.get("roles", [])
        or receipt.get("configuration") != trial.get("configuration", {})
        or not trial["start_ms"] <= receipt["observed_ms"] <= trial["end_ms"]
        or receipt.get("method") not in ("automated", "physical-review")
        or not receipt.get("before")
        or not receipt.get("after")
        or not receipt.get("artifacts")
        or not receipt.get("reviewer")
    ):
        raise ValueError(
            "Receipt lacks correlated action, state, configuration, artifacts, or review."
        )
    paths = [relative]
    kinds = set()
    for artifact in receipt["artifacts"]:
        support = artifact_path(directory, artifact["path"])
        if support == path or not support.stat().st_size:
            raise ValueError("Receipt must refer to nonempty independent raw evidence.")
        if hashlib.sha256(support.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("Raw artifact changed after review.")
        kinds.add(artifact["kind"])
        paths.append(artifact["path"])
    if role == "physical_visibility" and not kinds.intersection(
        {"physical-video", "physical-photo"}
    ):
        raise ValueError(
            "Capture-excluded images and native affinity do not prove physical visibility."
        )
    if role.endswith("transcript") and "transcript" not in kinds:
        raise ValueError(
            "Require actual transcript evidence, not the recording indicator."
        )
    if role == "physical_configuration":
        if (
            not kinds.intersection({"physical-video", "physical-photo"})
            or "native-display-snapshot" not in kinds
        ):
            raise ValueError(
                "Require physical configuration evidence and a native display snapshot."
            )
        snapshots = [
            item
            for item in receipt["artifacts"]
            if item["kind"] == "native-display-snapshot"
        ]
        snapshot = json.loads(
            artifact_path(directory, snapshots[-1]["path"]).read_text(encoding="utf-8")
        )
        monitors = snapshot["monitors"]
        configuration = trial.get("configuration", {})
        display = configuration.get("display")
        if display == "single-100" and (
            len(monitors) != 1 or monitors[0].get("dpi") != [96, 96]
        ):
            raise ValueError(
                "Native display snapshot does not establish single-monitor 100% scaling."
            )
        if display == "dual-100-150-secondary-left" and (
            len(monitors) != 2
            or sorted(monitor.get("dpi", [0])[0] for monitor in monitors) != [96, 144]
            or not any(
                not monitor["Flags"] & 1 and monitor["Monitor"][0] < 0
                for monitor in monitors
            )
        ):
            raise ValueError(
                "Native snapshot does not establish the required left-secondary mixed-DPI layout."
            )
        if abs(snapshot["time_ns"] // 1_000_000 - receipt["observed_ms"]) > 60000:
            raise ValueError(
                "Display snapshot is more than 60 seconds from the observed configuration."
            )
    return paths


def proof_errors(
    trial: dict[str, Any],
    events: list[dict[str, Any]],
    requirements: list[str],
    directory: Path | None,
) -> list[str]:
    """Return missing or invalid evidence while preserving independently observed leaks."""
    if directory is None:
        return ["artifact verification directory"]
    errors = []
    references = {**trial.get("proof", {}), "completion": trial.get("completion", "")}
    for role in [*requirements, "completion"]:
        try:
            validate_reference(references.get(role, ""), role, trial, events, directory)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            StopIteration,
            AttributeError,
        ) as error:
            errors.append(f"{role}: {error}")
    return errors


def observation_errors(
    trial: dict[str, Any],
    measured: list[dict[str, Any]],
    events: list[dict[str, Any]],
    directory: Path | None,
) -> list[str]:
    """Require actual probe output and source-matched calibration independently of receipts."""
    errors = []
    roles = trial.get("scenario", {}).get("proof", [])
    for event in measured:
        if event["type"] != "saved-frame":
            continue
        try:
            if directory is None:
                raise ValueError("No artifact directory.")
            artifact = event["data"]
            path = artifact_path(directory, artifact["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ValueError("Frame hash changed.")
        except (OSError, ValueError, KeyError, TypeError):
            errors.append("retained and unmodified action frame artifacts")
    for role, kinds in EVENT_PROOF.items():
        if (
            role in roles
            and role not in {"baseline", "positive_control", "capture_source"}
            and not any(event["type"] in kinds for event in measured)
        ):
            errors.append(f"actual {role} samples")
    starts = [event for event in measured if event["type"] == "interval-start"]
    if starts:
        data = starts[0].get("data", {})
        declared = trial.get("configuration", {})
        observed = data.get("configuration", {})
        if any(
            str(observed.get(key)).lower() != str(value).lower()
            for key, value in declared.items()
        ):
            errors.append("matching observed configuration")
        ua = data.get("userAgent", "")
        browser = declared.get("browser")
        if browser and not {
            "chrome": "Chrome/" in ua and "Edg/" not in ua,
            "edge": "Edg/" in ua,
            "firefox": "Firefox/" in ua,
        }.get(browser, False):
            errors.append("selected browser matching actual user agent")
        if declared.get("window_mode") in (
            "DOM-fullscreen",
            "fullscreen-video",
        ) and not data.get("fullscreen"):
            errors.append("actual browser fullscreen state")
    if "idle_probe" in roles:
        samples = [
            event.get("data", {}) for event in measured if event["type"] == "idle-state"
        ]
        if (
            trial.get("end_ms", 0) - trial.get("start_ms", 0) < 65000
            or not samples
            or any(item.get("user") not in ("active", "idle") for item in samples)
        ):
            errors.append("at least 65 seconds of valid system idle observations")
    if (
        "pressure_probe" in roles
        and sum(event["type"] == "pressure-sample" for event in measured) < 2
    ):
        errors.append(
            "multiple actual pressure samples; API availability is insufficient"
        )
    for role, event_type in (
        ("worklet_probe", "audio-quantum"),
        ("storage_probe", "storage-sample"),
        ("memory_probe", "memory-sample"),
        ("gpu_probe", "gpu-sample"),
    ):
        samples = [event for event in measured if event["type"] == event_type]
        if role in roles and (
            len(samples) < 3
            or role == "worklet_probe"
            and any(
                event.get("data", {}).get("state") != "running"
                or not event.get("data", {}).get("samples")
                for event in samples
            )
        ):
            errors.append(f"continuous live {role} output")
    if "camera_probe" in roles and not any(
        event["type"] == "saved-frame" and event.get("data", {}).get("kind") == "camera"
        for event in measured
    ):
        errors.append("retained actual camera frame, not a display capture")
    if "frames" not in roles or not starts:
        return errors
    epoch = starts[0].get("data", {}).get("calibration_epoch")
    setup = [
        event
        for event in events
        if event["wall_ms"] <= trial["start_ms"]
        and event.get("data", {}).get("calibration_epoch") == epoch
    ]
    if not epoch or any(
        not any(event["type"] == kind for event in setup)
        for kind in ("capture-baseline", "capture-positive", "capture-calibration")
    ):
        errors.append(
            "retained source-matched baseline, positive and restored negative calibration"
        )
    positive = [event for event in setup if event["type"] == "capture-positive"]
    negative = [event for event in setup if event["type"] == "capture-calibration"]
    if (
        not positive
        or not positive[-1]["data"].get("positive")
        or not negative
        or not negative[-1]["data"].get("calibrated")
        or negative[-1]["data"].get("stable_frames", 0) < 10
    ):
        errors.append("effective positive control and ten unchanged negative frames")
    actual_source = starts[0].get("data", {}).get("capture", {}).get("displaySurface")
    requested = trial.get("configuration", {}).get("capture_source")
    if requested == "Que-if-offered":
        requested = "window"
    if requested not in (None, "none") and actual_source != requested:
        errors.append("actual displaySurface matching requested source")
    if actual_source == "monitor" and (
        not positive or positive[-1]["data"].get("control") != "native"
    ):
        errors.append("native positive control for monitor capture")
    frames = [event for event in measured if event["type"] == "capture-frame"]
    if any(
        event.get("data", {}).get("calibration_epoch") != epoch
        or event.get("data", {}).get("callback") != "video"
        for event in frames
    ):
        errors.append("continuous decoded-frame callbacks in one calibration epoch")
    for kind in ("baseline", "positive-control", "restored-negative-control"):
        saved = [
            event
            for event in setup
            if event["type"] == "saved-frame" and event["data"].get("kind") == kind
        ]
        if not saved or directory is None:
            errors.append(f"retained {kind} image")
            continue
        try:
            artifact = saved[-1]["data"]
            path = artifact_path(directory, artifact["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                errors.append(f"uncorrupted {kind} image")
        except (OSError, ValueError, KeyError):
            errors.append(f"readable {kind} image")
    if not any(event["type"] == "saved-frame" for event in measured):
        errors.append("retained action frame artifacts")
    return errors
