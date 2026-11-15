"""Correlate native samples after collection without exposing them to page scripts."""

import itertools
import json
from pathlib import Path
from typing import Any

from stealth.results import Result, atomic_json, read_json


def summarize_native(directory: Path) -> list[Result]:
    """Report observed z-order overlap durations and the actual sampling resolution."""
    path = directory / "native" / "windows.jsonl"
    if not path.exists():
        return [
            Result(
                "native.observer",
                "INCONCLUSIVE",
                "No native window timeline was recorded.",
            )
        ]
    try:
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        samples = [record for record in records if "interval_ms" in record]
        if not samples or any("error" in record for record in records):
            raise ValueError("Missing samples or native logger errors.")
        if not any(sample["windows"] for sample in samples):
            raise ValueError("No target window was observed.")
        if not any(record.get("winevent_installed") for record in records):
            raise ValueError("Window-order event hook was not installed.")
        socket_path = directory / "native" / "sockets.jsonl"
        socket_rows = [
            json.loads(line)
            for line in socket_path.read_text(encoding="utf-8").splitlines()
        ]
        if any("error" in row for row in socket_rows) or not any(
            row.get("processes") for row in socket_rows
        ):
            raise ValueError("Native process/socket inventory is incomplete.")
        episodes: list[dict[str, Any]] = []
        active: dict[int, int] = {}
        for sample in samples:
            occluded = {
                window["hwnd"]
                for window in sample["windows"]
                if window["potential_occluders"]
            }
            for hwnd in occluded - active.keys():
                active[hwnd] = sample["wall_ms"]
            for hwnd in list(active.keys() - occluded):
                start = active.pop(hwnd)
                episodes.append(
                    {
                        "hwnd": hwnd,
                        "start_ms": start,
                        "end_ms": sample["wall_ms"],
                        "duration_ms": sample["wall_ms"] - start,
                    }
                )
        for hwnd, start in active.items():
            episodes.append(
                {
                    "hwnd": hwnd,
                    "start_ms": start,
                    "end_ms": None,
                    "duration_ms": samples[-1]["wall_ms"] - start,
                }
            )
        gaps = [b["wall_ms"] - a["wall_ms"] for a, b in itertools.pairwise(samples)]
        checkpoint = directory / "checkpoint.json"
        coverage_gaps = []
        if checkpoint.exists():
            for attempt in read_json(checkpoint)["attempts"].values():
                if not attempt.get("end_ms"):
                    continue
                times = [
                    attempt["start_ms"],
                    *[
                        sample["wall_ms"]
                        for sample in samples
                        if attempt["start_ms"] <= sample["wall_ms"] <= attempt["end_ms"]
                    ],
                    attempt["end_ms"],
                ]
                if any(end - start > 250 for start, end in itertools.pairwise(times)):
                    coverage_gaps.append(attempt["attempt"])
        summary = {
            "sample_count": len(samples),
            "max_interval_ms": max(gaps, default=0),
            "mean_interval_ms": sum(gaps) / len(gaps) if gaps else None,
            "potential_occlusions": episodes,
            "attempts_with_sampling_gaps_over_250ms": coverage_gaps,
            "interpretation": "Rectangle overlap is a native diagnostic; transparency/cloaking can produce candidates. Correlate physical confirmation before claiming physical occlusion. Events shorter than sample gaps may be missed. No timer exemption applies.",
        }
        atomic_json(directory / "native" / "summary.json", summary)
        return [
            Result(
                "native.sampling",
                "INCONCLUSIVE" if coverage_gaps else "PASS",
                "Collected independent window samples; physical/topmost conclusions require the scenario's guided evidence.",
                evidence=[
                    "native/summary.json",
                    "native/windows.jsonl",
                    "native/sockets.jsonl",
                ],
            )
        ]
    except (OSError, ValueError, KeyError) as error:
        return [
            Result(
                "native.observer",
                "INCONCLUSIVE",
                f"Native evidence is incomplete: {error}",
            )
        ]
