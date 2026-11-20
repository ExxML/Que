"""Compare received calibration audio across real Que audio acquisition."""

import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from stealth.results import Result

from capture.detector import Detector
from capture.media import executable


def audio_result(
    path: Path, detector: Detector, states: list[dict[str, Any]], ffmpeg: str
) -> Result:
    """Require the received tone before, during, and after microphone/loopback capture."""
    if len(states) != 3 or not states[1].get("recording") or states[2].get("recording"):
        return Result(
            "audio",
            "INCONCLUSIVE",
            "Que audio acquisition did not start and stop successfully.",
            kind="capture",
        )
    health = states[1].get("source_health", {})
    if sorted(states[1].get("audio_sources", [])) != ["them", "you"] or any(
        health.values()
    ):
        return Result(
            "audio",
            "INCONCLUSIVE",
            "Real microphone and loopback health were not both established.",
            kind="capture",
        )
    measurements = []
    for phase in (80, 81, 82):
        observed = detector.observations.get(phase)
        if (
            observed is None
            or observed.first is None
            or observed.last is None
            or observed.last - observed.first < 2
        ):
            return Result(
                "audio",
                "INCONCLUSIVE",
                "Missing audio measurement interval in the received video timeline.",
                kind="capture",
            )
        result = subprocess.run(
            [
                executable(ffmpeg),
                "-hide_banner",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-ss",
                str(observed.first + 0.3),
                "-t",
                str(observed.last - observed.first - 0.6),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "f32le",
                "pipe:1",
            ],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode or len(result.stdout) < 16000 * 4:
            return Result(
                "audio",
                "INCONCLUSIVE",
                "Receiver recording has no complete shared-audio control.",
                kind="capture",
            )
        samples = np.frombuffer(result.stdout, dtype=np.float32)
        if not np.all(np.isfinite(samples)):
            return Result(
                "audio",
                "INCONCLUSIVE",
                "Invalid decoded audio samples.",
                kind="capture",
            )
        blocks = [
            samples[start : start + 1600]
            for start in range(0, len(samples) - 1599, 1600)
        ]
        energies = [float(np.sqrt(np.mean(block**2))) for block in blocks]
        spectrum = np.abs(np.fft.rfft(samples))
        frequencies = np.fft.rfftfreq(len(samples), 1 / 16000)
        tone = float(
            np.sum(spectrum[(frequencies > 425) & (frequencies < 455)] ** 2)
            / max(float(np.sum(spectrum**2)), 1e-20)
        )
        measurements.append((float(np.median(energies)), min(energies), tone))
    if any(
        item[0] < 0.001 or item[2] < 0.6 for item in (measurements[0], measurements[2])
    ):
        return Result(
            "audio",
            "INCONCLUSIVE",
            "The received 440 Hz audio control was not established before and after Que capture.",
            kind="capture",
        )
    baseline = (measurements[0][0] + measurements[2][0]) / 2
    active = measurements[1]
    failed = (
        active[1] < baseline * 0.25
        or not baseline * 0.5 <= active[0] <= baseline * 2
        or active[2] < 0.6
    )
    return Result(
        "audio",
        "FAIL" if failed else "PASS",
        "Received shared audio changed or dropped while Que captured audio."
        if failed
        else "Calibrated shared audio continued through Que microphone/loopback start and stop.",
        kind="capture",
        configuration={
            "measurements": measurements,
            "scope": "Shared-system audio continuity; no acoustic microphone-content certification.",
        },
    )
