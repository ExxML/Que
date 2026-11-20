"""Decode original recording frames with their actual presentation timestamps."""

from __future__ import annotations

import importlib
import queue
import re
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from capture.detector import Frame


def executable(configured: str = "ffmpeg") -> str:
    """Resolve an explicit FFmpeg command or the pinned test-only binary package."""
    import shutil

    found = shutil.which(configured)
    if found:
        return found
    if configured != "ffmpeg":
        raise FileNotFoundError(f"FFmpeg executable is unavailable: {configured}")
    return str(importlib.import_module("imageio_ffmpeg").get_ffmpeg_exe())


def frames(path: Path, ffmpeg: str = "ffmpeg") -> Iterator[tuple[Frame, float]]:
    """Decode without frame-rate conversion and reject missing timestamps or partial frames."""
    decoder = executable(ffmpeg)
    metadata = subprocess.run(
        [decoder, "-hide_banner", "-i", str(path)],
        capture_output=True,
        check=False,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    description = metadata.stderr.decode("utf-8", errors="replace")
    video = next(
        (
            line
            for line in description.splitlines()
            if "Stream #" in line and "Video:" in line
        ),
        "",
    )
    dimensions = re.search(r"[, ](\d{2,5})x(\d{2,5})(?:[ ,\[])", video)
    if dimensions is None:
        raise ValueError("Recording has no decodable video stream.")
    width, height = map(int, dimensions.groups())
    if width * height > 4096 * 4096:
        raise ValueError("Video dimensions exceed the audit decoder's memory bound.")
    process = subprocess.Popen(
        [
            decoder,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "showinfo",
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    times: queue.Queue[float | None] = queue.Queue()
    pixels: queue.Queue[bytes | None] = queue.Queue(maxsize=3)
    stopped = threading.Event()
    errors: list[str] = []

    def timestamps() -> None:
        """Drain FFmpeg diagnostics while keeping each decoded frame's PTS."""
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace")
            match = re.search(r"\bn:\s*\d+.*\bpts_time:([\d.eE+-]+)", line)
            if match:
                times.put(float(match.group(1)))
            elif "Error" in line or "Invalid" in line:
                errors.append(line.strip())
        times.put(None)

    def read() -> None:
        """Read complete frame payloads with cancellation and bounded buffering."""
        assert process.stdout is not None
        while not stopped.is_set():
            data = process.stdout.read(width * height * 3)
            while not stopped.is_set():
                try:
                    pixels.put(data or None, timeout=0.2)
                    break
                except queue.Full:
                    continue
            if not data:
                break

    threads = [
        threading.Thread(target=timestamps, daemon=True),
        threading.Thread(target=read, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while True:
            data = pixels.get(timeout=30)
            if data is None:
                break
            timestamp = times.get(timeout=30)
            if timestamp is None or len(data) != width * height * 3:
                raise ValueError("Truncated frame or missing presentation timestamp.")
            yield (
                np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3),
                timestamp,
            )
        returncode = process.wait(timeout=10)
        if returncode != 0 or errors:
            raise ValueError(
                f"FFmpeg decoding failed ({returncode}): {'; '.join(errors)[:1200]}"
            )
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def save_frame(path: Path, frame: Frame) -> None:
    """Save lossless RGB evidence using the existing Qt image dependency."""
    from PyQt6.QtGui import QImage

    data = np.ascontiguousarray(frame)
    image = QImage(
        data.tobytes(),
        data.shape[1],
        data.shape[0],
        data.shape[1] * 3,
        QImage.Format.Format_RGB888,
    )
    if not image.save(str(path)):
        raise OSError(f"Could not write frame evidence {path.name}.")


def media_metadata(path: Path, ffprobe: str) -> dict[str, Any]:
    """Inspect stream information when the separately installed ffprobe is available."""
    import json
    import shutil

    if not shutil.which(ffprobe):
        return {
            "ffprobe": "unavailable; decoded PTS and fixture identity remain required"
        }
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return dict(json.loads(result.stdout))
