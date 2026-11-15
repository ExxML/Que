"""Derive registered timing comparisons from complete browser measurement intervals."""

import hashlib
import itertools
import json
import math
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Any

from stealth.analysis import paired_statistics
from stealth.results import Result, Status, assess_trial, atomic_json, read_json

METRICS = {
    "animation": "interval_ms",
    "worker-timing": "interval_ms",
    "heartbeat": "timer_delay_ms",
    "owned-endpoint": "duration",
    "render-workload": "duration",
    "gpu-sample": "duration",
    "gpu-timer": "gpu_ms",
    "audio-quantum": "received",
    "storage-sample": "duration",
    "memory-sample": "duration",
}
CONDITIONS = (
    "absent",
    "idle",
    "screenshot",
    "recording",
    "transcribing",
    "streaming",
    "cpu-control",
    "network-control",
)


def analyze_statistics(directory: Path) -> list[Result]:
    """Compute paired metrics from registered, reviewed intervals, never typed-in measurements."""
    schedule = read_json(directory / "statistics-schedule.json")
    path = directory / "checkpoint.json"
    state = read_json(path) if path.exists() else {"attempts": {}}
    intervals: dict[str, dict[str, Any]] = {}
    sessions: dict[str, list[dict[str, Any]]] = {}
    for attempt in state["attempts"].values():
        if attempt["scenario"].get("family") == "statistics":
            intervals[attempt["scenario"]["id"]] = attempt
    results = []
    comparisons = len(schedule) * len(METRICS) * 3
    summaries: dict[str, dict[str, Any]] = {}
    for experiment, orders in schedule.items():
        for event_type, metric in METRICS.items():
            for reducer in ("mean", "p95", "stdev"):
                batches: list[list[dict[str, Any]]] = []
                experiment_configuration: str | None = None
                try:
                    for batch_index, batch in enumerate(orders):
                        pairs = []
                        for pair_index, order in enumerate(batch):
                            pair: dict[str, Any] = {"order": order}
                            observed_starts = {}
                            for phase in ("baseline", "action"):
                                identity = f"stat.{experiment}.{batch_index + 1}.{pair_index + 1}.{phase}"
                                trial = intervals[identity]
                                if (
                                    not trial.get("completion")
                                    or not trial.get("review")
                                    or not trial.get("end_ms")
                                    or trial.get("transport_error")
                                ):
                                    raise ValueError(
                                        "Unreviewed or interrupted interval."
                                    )
                                session = trial["session"]
                                if session not in sessions:
                                    sessions[session] = [
                                        json.loads(line)
                                        for line in (
                                            directory / "browser" / f"{session}.jsonl"
                                        )
                                        .read_text(encoding="utf-8")
                                        .splitlines()
                                    ]
                                events = [
                                    event
                                    for event in sessions[session]
                                    if event.get("attempt") == trial["attempt"]
                                    and trial["start_ms"]
                                    <= event["wall_ms"]
                                    <= trial["end_ms"]
                                ]
                                if (
                                    assess_trial(
                                        trial,
                                        events,
                                        trial["scenario"]["proof"],
                                        directory,
                                        sessions[session],
                                    ).status
                                    != "PASS"
                                ):
                                    raise ValueError(
                                        "Incomplete action evidence or observer coverage."
                                    )
                                if any(
                                    event["type"]
                                    in (
                                        "logger-error",
                                        "observer-error",
                                        "pagehide",
                                        "overflow",
                                    )
                                    for event in events
                                ):
                                    raise ValueError("Unhealthy statistical observer.")
                                values = [
                                    float(event["data"][metric])
                                    for event in events
                                    if event["type"] == event_type
                                ]
                                if len(values) < 5:
                                    raise ValueError(
                                        "At least five samples per interval/metric are required."
                                    )
                                if not all(math.isfinite(value) for value in values):
                                    raise ValueError("Nonfinite metric samples.")
                                if trial["end_ms"] - trial["start_ms"] < 15000:
                                    raise ValueError(
                                        "Statistical intervals require at least 15 seconds."
                                    )
                                start = next(
                                    event
                                    for event in events
                                    if event["type"] == "interval-start"
                                )
                                signature = json.dumps(
                                    {
                                        "configuration": trial.get("configuration", {}),
                                        "userAgent": start.get("data", {}).get(
                                            "userAgent"
                                        ),
                                    },
                                    sort_keys=True,
                                )
                                if (
                                    experiment_configuration is not None
                                    and signature != experiment_configuration
                                ):
                                    raise ValueError(
                                        "Configuration or browser version changed within the comparison."
                                    )
                                experiment_configuration = signature
                                if experiment.endswith(
                                    ("cpu-control", "network-control")
                                ):
                                    field = (
                                        "cpu_control"
                                        if experiment.endswith("cpu-control")
                                        else "network_control"
                                    )
                                    if start.get("data", {}).get(field) is not (
                                        phase == "action"
                                    ):
                                        raise ValueError(
                                            "The preregistered control workload was not active in the requested phase."
                                        )
                                if event_type == "audio-quantum":
                                    values = [
                                        right - left
                                        for left, right in itertools.pairwise(values)
                                    ]
                                pair[phase] = (
                                    statistics.mean(values)
                                    if reducer == "mean"
                                    else sorted(values)[
                                        math.ceil(len(values) * 0.95) - 1
                                    ]
                                    if reducer == "p95"
                                    else statistics.stdev(values)
                                )
                                observed_starts[phase] = trial["start_ms"]
                            if (
                                observed_starts["baseline"] < observed_starts["action"]
                            ) != (order == "AB"):
                                raise ValueError(
                                    "Actual interval order differs from the registered randomized order."
                                )
                            pairs.append(pair)
                        batches.append(pairs)
                    summary = paired_statistics(batches, comparisons)
                    summary["equivalence_bound_ms"] = 1.0
                    summary["reducer"] = reducer
                    summaries[f"statistics.{experiment}.{event_type}.{reducer}"] = (
                        summary
                    )
                    fingerprint = hashlib.sha256(
                        json.dumps(summary, sort_keys=True).encode()
                    ).hexdigest()[:16]
                    name = f"statistics-{experiment}-{event_type}-{reducer}-{fingerprint}.json"
                    atomic_json(directory / name, summary)
                    control = experiment.endswith(("cpu-control", "network-control"))
                    status: Status = (
                        "FAIL"
                        if summary["potential_inference_channel"] and not control
                        else "PASS"
                        if (
                            summary["potential_inference_channel"]
                            if control
                            else all(
                                -1.0
                                <= batch["interval"][0]
                                <= batch["interval"][1]
                                <= 1.0
                                for batch in summary["batches"]
                            )
                        )
                        else "INCONCLUSIVE"
                    )
                    results.append(
                        Result(
                            f"statistics.{experiment}.{event_type}.{reducer}",
                            status,
                            "Load-control sensitivity measured; an ineffective control cannot validate a negative."
                            if control
                            else "Potential inference channel; identification is not established."
                            if status == "FAIL"
                            else "No reproduced difference within the preregistered ±1 ms bound for this metric/reducer; controls must also pass."
                            if status == "PASS"
                            else "Insufficient sensitivity to exclude the preregistered ±1 ms effect, or ineffective load control.",
                            "observable effect",
                            evidence=[name],
                        )
                    )
                except (OSError, ValueError, KeyError, TypeError) as error:
                    results.append(
                        Result(
                            f"statistics.{experiment}.{event_type}.{reducer}",
                            "INCONCLUSIVE",
                            f"Incomplete registered measurements: {error}",
                            "observable effect",
                        )
                    )
    lookup = {result.check: result for result in results}
    for result in results:
        if result.status != "PASS" or "control." in result.check:
            continue
        experiment = result.check.split(".")[1]
        browser = experiment.split("-")[0]
        suffix = ".".join(result.check.split(".")[2:])
        control = "network-control" if "owned-endpoint" in suffix else "cpu-control"
        needed = [f"statistics.{browser}-{control}.{suffix}"]
        if not experiment.endswith("-absent"):
            needed.append(f"statistics.{browser}-absent.{suffix}")
        if any(name not in lookup or lookup[name].status != "PASS" for name in needed):
            result.status = "INCONCLUSIVE"
            result.reason = "Negative result lacks an effective matched load control and stable absent/sham baseline."
    history_path = directory / "statistical-failures.json"
    history = read_json(history_path) if history_path.exists() else []
    identities = {(item["check"], tuple(item["evidence"])) for item in history}
    for result in results:
        if (
            result.status == "FAIL"
            and (result.check, tuple(result.evidence)) not in identities
        ):
            history.append(asdict(result))
    atomic_json(history_path, history)
    current_failures = {
        (result.check, tuple(result.evidence))
        for result in results
        if result.status == "FAIL"
    }
    for item in history:
        if (item["check"], tuple(item["evidence"])) not in current_failures:
            historical = Result(**item)
            historical.check += ".retained-failure"
            results.append(historical)
    return results
