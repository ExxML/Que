"""Measure live acquisition health without retaining audio or invoking transcription."""

import json
import queue
import sys
import tempfile
import threading
import time
import wave
import winsound
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import ctranslate2  # type: ignore[import-untyped]  # noqa: F401
import numpy as np
from PyQt6.QtCore import QCoreApplication

from core.settings import SettingsStore
from core.transcriber import Transcriber, _to_mono_16k

Counters = dict[str, dict[str, int]]


def run() -> list[dict[str, object]]:
    """Exercise both acquisition orders twice, keeping only counts and health messages."""
    app = QCoreApplication([])
    results = []
    with tempfile.TemporaryDirectory() as directory:
        transcriber = Transcriber(SettingsStore(Path(directory) / "config.json"))
        playback = "--silent-playback" in sys.argv
        silence = Path(directory) / "silence.wav"
        if playback:
            with wave.open(str(silence), "wb") as wav:
                wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                wav.writeframes(bytes(48000 * 4 * 8))
        for order in (("you", "them"), ("them", "you")) * 2:
            stop = threading.Event()
            utterances: queue.Queue[tuple[str, Any] | None] = queue.Queue()
            counters: Counters = {
                source: {"chunks": 0, "bytes": 0, "nonzero_chunks": 0}
                for source in order
            }
            current_source = [""]
            original_poll = transcriber._poll_source
            errors: list[dict[str, object]] = []
            statuses: list[dict[str, object]] = []

            def failed(
                message: str, fatal: bool, errors: list[dict[str, object]] = errors
            ) -> None:
                """Retain errors without treating unavailable hardware as healthy capture."""
                errors.append({"message": message, "fatal": fatal})

            def status(
                source: str,
                message: str,
                _session: object,
                statuses: list[dict[str, object]] = statuses,
            ) -> None:
                """Retain source-health transitions only."""
                statuses.append({"source": source, "message": message})

            def measure(
                data: bytes,
                channels: int,
                rate: int,
                counters: Counters = counters,
                current_source: list[str] = current_source,
            ) -> Any:
                """Count received samples without preserving their content."""
                source = current_source[0]
                samples = _to_mono_16k(data, channels, rate)
                counters[source]["chunks"] += 1
                counters[source]["bytes"] += len(data)
                counters[source]["nonzero_chunks"] += int(
                    bool(samples.size) and bool(np.any(samples))
                )
                return samples

            def poll(
                capture: Any,
                stop_event: threading.Event,
                current_source: list[str] = current_source,
                original_poll: Any = original_poll,
            ) -> Any:
                """Attribute processing counters to the actual source being polled."""
                current_source[0] = capture.source
                return original_poll(capture, stop_event)

            transcriber.failed.connect(failed)
            transcriber.source_status.connect(status)
            threads = [
                threading.Thread(
                    target=transcriber._capture_sources,
                    args=(order, stop, utterances),
                    daemon=True,
                )
            ]
            with (
                patch("core.transcriber._to_mono_16k", side_effect=measure),
                patch.object(transcriber, "_poll_source", side_effect=poll),
            ):
                start = time.monotonic()
                if playback:
                    winsound.PlaySound(
                        str(silence),
                        winsound.SND_FILENAME
                        | winsound.SND_ASYNC
                        | winsound.SND_NODEFAULT,
                    )
                threads[0].start()
                while time.monotonic() - start < 6:
                    app.processEvents()
                    time.sleep(0.02)
                stop.set()
                for thread in threads:
                    thread.join(5)
                app.processEvents()
                if playback:
                    winsound.PlaySound(None, 0)
            items = []
            while not utterances.empty():
                items.append(utterances.get_nowait())
            results.append(
                {
                    "order": order,
                    "counts": counters,
                    "errors": errors,
                    "statuses": statuses,
                    "silent_playback": playback,
                    "completed_sources": sum(item is None for item in items),
                    "threads_stopped": all(not thread.is_alive() for thread in threads),
                    "scope": "Live devices; optional zero-valued playback control, no browser co-capture, no speech recognition, no retained audio.",
                }
            )
            items.clear()
            transcriber.failed.disconnect(failed)
            transcriber.source_status.disconnect(status)
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
