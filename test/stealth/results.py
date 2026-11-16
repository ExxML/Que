"""Persist auditable results and reject incomplete negative observations."""

import html
import itertools
import json
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from stealth.evidence import observation_errors, proof_errors

Status = Literal["PASS", "FAIL", "INCONCLUSIVE", "UNSUPPORTED"]


@dataclass
class Result:
    """Describe one measured check and its exact scope."""

    check: str
    status: Status
    reason: str
    kind: str = "native diagnostic"
    configuration: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    required: bool = True


def atomic_json(path: Path, value: Any) -> None:
    """Replace a JSON checkpoint after flushing its complete contents."""
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    # Windows background scanners briefly lock freshly written files (~1 in 4,000 replaces).
    for remaining in range(40, -1, -1):
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            return
        except PermissionError:
            if not remaining:
                raise
            time.sleep(0.025)


def read_json(path: Path) -> Any:
    """Read a UTF-8 JSON evidence file."""
    return json.loads(path.read_text(encoding="utf-8"))


def verdict(results: list[Result]) -> tuple[str, int]:
    """Return a failure-first summary without converting missing coverage to success."""
    if any(result.status == "FAIL" for result in results):
        return "FAIL", 1
    if not results or any(
        result.required and result.status != "PASS" for result in results
    ):
        return "INCOMPLETE", 2
    return "PASS", 0


def assess_trial(
    trial: dict[str, Any],
    events: list[dict[str, Any]],
    requirements: list[str],
    directory: Path | None = None,
    session_events: list[dict[str, Any]] | None = None,
) -> Result:
    """Require action, observer health, calibration, and reviewed evidence for a pass."""
    configuration = trial.get("configuration", {})
    result = Result(
        trial["id"], "INCONCLUSIVE", "Unfinished trial.", "browser", configuration
    )
    result.evidence = trial.get("evidence", [])
    result.configuration = {
        **configuration,
        "observed_event_types": sorted({event["type"] for event in events}),
    }
    if trial.get("leaks"):
        result.status, result.reason = "FAIL", "; ".join(trial["leaks"])
        return result
    if trial.get("unsupported"):
        result.status, result.reason = "UNSUPPORTED", str(trial["unsupported"])
        return result
    missing = proof_errors(trial, session_events or events, requirements, directory)
    missing.extend(
        observation_errors(trial, events, session_events or events, directory)
    )
    if not trial.get("completion"):
        missing.append("action completion evidence")
    if not trial.get("review"):
        missing.append("evidence review")
    if not trial.get("end_ms") or not trial.get("start_ms"):
        missing.append("complete measurement interval")
    elif trial["end_ms"] - trial["start_ms"] < 2000:
        missing.append("measurement interval of at least two seconds")
    if not events or not any(event["type"] == "heartbeat" for event in events):
        missing.append("observer heartbeat")
    if any(
        event["type"]
        in (
            "logger-error",
            "overflow",
            "pagehide",
            "observer-error",
            "probe-error",
            "rtc-error",
        )
        for event in events
    ):
        missing.append("healthy observer")
    times = (
        [trial.get("start_ms", 0)]
        + [event["wall_ms"] for event in events if event["type"] == "heartbeat"]
        + [trial.get("end_ms", 0)]
    )
    if any(b - a > 3500 or b < a for a, b in itertools.pairwise(times)):
        missing.append("continuous heartbeat (maximum gap 3.5 seconds)")
    if trial.get("transport_error"):
        missing.append("lossless evidence transport")
    if any(
        event["type"] in ("blur", "pagehide")
        or event.get("data", {}).get("hasFocus") is False
        for event in events
    ):
        missing.append("focused observer throughout the interval")
    starts = [event for event in events if event["type"] == "interval-start"]
    ends = [event for event in events if event["type"] == "interval-end"]
    if len(starts) != 1 or len(ends) != 1:
        missing.append("exactly one durable start/end marker")
    elif (
        starts[0]["wall_ms"] != trial.get("start_ms")
        or ends[0]["wall_ms"] != trial.get("end_ms")
        or not starts[0].get("data", {}).get("hasFocus")
        or starts[0].get("data", {}).get("visibility") != "visible"
        or starts[0]["seq"] >= ends[0]["seq"]
    ):
        missing.append("focused, visible, fully bounded observer interval")
    if ends and ends[0].get("data", {}).get("transportFailed"):
        missing.append("uninterrupted transport during measurement")
    if missing:
        result.reason = "Missing: " + ", ".join(missing)
    else:
        result.status = "PASS"
        result.reason = f"Reviewed bounded observation: {trial['review']}"
    return result


def write_reports(
    directory: Path, metadata: dict[str, Any], results: list[Result]
) -> int:
    """Write JSON, JUnit, and a self-contained HTML index with evidence links."""
    if (
        metadata.get("mode") == "full"
        and metadata.get("package_layout") == "onefile"
        and not any(result.check == "packaged.runtime-paths" for result in results)
    ):
        results = [
            *results,
            Result(
                "packaged.runtime-paths",
                "INCONCLUSIVE",
                "Custom/single-file runtime data paths have not been verified; use the project's standalone distribution for release validation.",
            ),
        ]
    summary, code = verdict(results)
    payload = {
        "schema": 1,
        "updated_ms": time.time_ns() // 1_000_000,
        "summary": summary,
        "metadata": metadata,
        "results": [asdict(result) for result in results],
    }
    atomic_json(directory / "report.json", payload)
    suite = ET.Element(
        "testsuite",
        name="Que browser observability",
        tests=str(len(results)),
        failures=str(sum(r.status == "FAIL" for r in results)),
        skipped=str(sum(r.status in ("INCONCLUSIVE", "UNSUPPORTED") for r in results)),
    )
    rows = []
    for result in results:
        case = ET.SubElement(
            suite, "testcase", name=result.check, classname=result.kind
        )
        if result.status != "PASS":
            ET.SubElement(
                case,
                "failure" if result.status == "FAIL" else "skipped",
                message=result.reason,
                type=result.status,
            ).text = result.reason
        ET.SubElement(case, "system-out").text = json.dumps(asdict(result))
        links = " ".join(
            f'<a href="{html.escape(path, quote=True)}">evidence</a>'
            for path in result.evidence
        )
        rows.append(
            f"<tr><td>{html.escape(result.check)}</td><td>{result.status}</td>"
            f"<td>{html.escape(result.reason)}</td><td><pre>{html.escape(json.dumps(result.configuration, indent=2))}</pre>{links}</td></tr>"
        )
    ET.ElementTree(suite).write(
        directory / "junit.xml", encoding="utf-8", xml_declaration=True
    )
    (directory / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Que observability audit</title>'
        "<style>body{font:16px system-ui;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #888;padding:.5rem;text-align:left}pre{white-space:pre-wrap}</style>"
        f"<h1>Que observability audit: {summary}</h1>"
        "<p>Measured protections, exposures, and coverage gaps. This is not a certificate of universal invisibility. "
        "Native diagnostics are not website capabilities. Required unsupported checks leave validation incomplete.</p>"
        + (
            '<p><a href="guide.html">Action reproduction and cleanup guide</a></p>'
            if (directory / "guide.html").exists()
            else ""
        )
        + f"<details><summary>Configuration</summary><pre>{html.escape(json.dumps(metadata, indent=2))}</pre></details>"
        "<table><tr><th>Check</th><th>Result</th><th>Finding</th><th>Configuration / evidence</th></tr>"
        + "".join(rows)
        + "</table>",
        encoding="utf-8",
    )
    return code


def write_guide(directory: Path, specifications: list[dict[str, Any]]) -> None:
    """Write reproduction and cleanup contracts once alongside the full matrix report."""
    atomic_json(directory / "manifest.json", specifications)
    rows = []
    for item in specifications:
        rows.append(
            f"<h2>{html.escape(item['id'])}</h2><p>{html.escape(item['action'])}</p>"
            f"<p>Prerequisites: {html.escape(', '.join(item['prerequisites']))}</p>"
            f"<p>Completion evidence: {html.escape(item['completion'])}</p>"
            f"<p>Observe: {html.escape(item['observations'])}</p>"
            f"<p>Protection: {html.escape(item['protection'])}</p>"
            f"<p>Required proof: {html.escape(', '.join(item['proof']))}</p>"
            f"<p>Cleanup: {html.escape(item['cleanup'])}</p>"
        )
    (directory / "guide.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Audit reproduction guide</title><h1>Reproduction guide</h1>'
        + "".join(rows),
        encoding="utf-8",
    )
