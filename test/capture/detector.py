"""Analyze calibrated receiver frames without treating compression noise as a clean capture."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

Frame = NDArray[np.uint8]
WIDTH = 960
HEIGHT = 720
COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255))
CONTROL_PHASES = (2, 3, 4, 5)
# Live Chrome/Edge VP8 captures drift up to about 9 smoothed levels after calibration,
# the weakest positive control moves about 31, and exposed Que surfaces at least 108.
NOISE_MARGIN = 20.0


@dataclass
class Observation:
    """Retain frame counts and worst observations for one visual phase."""

    frames: int = 0
    changed: int = 0
    maximum: float = 0.0
    ticks: set[int] = field(default_factory=set)
    first: float | None = None
    last: float | None = None
    largest_gap: float = 0.0


def normalize(frame: Frame) -> Frame:
    """Locate four colored fiducials and normalize the captured fixture geometry."""
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError("Expected an RGB uint8 frame.")
    points = []
    step = max(1, min(frame.shape[0] // 360, frame.shape[1] // 480))
    image = frame[::step, ::step].astype(np.int16)
    # A marker is at most 24 fixture pixels wide even when the fixture fills the frame.
    radius = 24 * frame.shape[1] / WIDTH / step
    for color, (corner_x, corner_y) in zip(COLORS, ((0, 0), (1, 0), (0, 1), (1, 1))):
        mask = np.max(np.abs(image - np.array(color)), axis=2) < 65
        marker_y, marker_x = np.nonzero(mask)
        if len(marker_x) < 6:
            raise ValueError("Missing or occluded fixture marker.")
        # Matching colors elsewhere (a cursor, compression fringes) must not bias the marker,
        # so keep only the cluster nearest the marker's own corner.
        nearest = np.argmin(
            np.hypot(
                marker_x - corner_x * (image.shape[1] - 1),
                marker_y - corner_y * (image.shape[0] - 1),
            )
        )
        cluster = (
            np.hypot(marker_x - marker_x[nearest], marker_y - marker_y[nearest])
            <= radius
        )
        if np.count_nonzero(cluster) < 6:
            raise ValueError("Missing or occluded fixture marker.")
        points.append(
            (
                float(np.median(marker_x[cluster])) * step,
                float(np.median(marker_y[cluster])) * step,
            )
        )
    left = (points[0][0] + points[2][0]) / 2
    right = (points[1][0] + points[3][0]) / 2
    top = (points[0][1] + points[1][1]) / 2
    bottom = (points[2][1] + points[3][1]) / 2
    if right - left < 450 or bottom - top < 320:
        raise ValueError("Received fixture is too small for calibrated comparison.")
    if (
        max(
            abs(points[0][0] - points[2][0]),
            abs(points[1][0] - points[3][0]),
            abs(points[0][1] - points[1][1]),
            abs(points[2][1] - points[3][1]),
        )
        > 5
    ):
        raise ValueError("Ambiguous fixture markers or distorted capture.")
    xs = left + (np.arange(WIDTH) - 16) * (right - left) / (WIDTH - 32)
    ys = top + (np.arange(HEIGHT) - 16) * (bottom - top) / (HEIGHT - 32)
    if (
        xs[0] < -2
        or ys[0] < -2
        or xs[-1] >= frame.shape[1] + 2
        or ys[-1] >= frame.shape[0] + 2
    ):
        raise ValueError("Fixture is cropped.")
    xi = np.clip(np.rint(xs).astype(int), 0, frame.shape[1] - 1)
    yi = np.clip(np.rint(ys).astype(int), 0, frame.shape[0] - 1)
    result: Frame = frame[yi[:, None], xi[None, :]].copy()
    return result


def identity(frame: Frame) -> tuple[int, int, int]:
    """Decode the source nonce, phase and liveness counter from the fixture header."""
    values = []
    for y in (36, 56):
        value = 0
        for bit in range(32):
            light = float(
                np.mean(frame[y + 3 : y + 9, 64 + bit * 12 + 3 : 64 + bit * 12 + 9])
            )
            if 90 < light < 165:
                raise ValueError("Ambiguous fixture identity bits.")
            value = (value << 1) | int(light >= 165)
        values.append(value)
    return values[0], values[1] >> 24, values[1] & 0xFFFFFF


def features(frame: Frame) -> NDArray[np.float64]:
    """Measure small tiles without averaging a transient across multiple frames."""
    pixels = frame.astype(np.float64)
    # A 2x2 box filter halves single-pixel codec ringing at checker corners, while a
    # one-pixel line still moves its tile's extreme by about half its contrast.
    pixels = (
        pixels
        + np.roll(pixels, 1, axis=0)
        + np.roll(pixels, 1, axis=1)
        + np.roll(pixels, (1, 1), axis=(0, 1))
    ) / 4
    # Start one tile below the header: its per-frame identity bits make the edge ring.
    roi = pixels[104:688, 32:928]
    # Both tile means and local extrema preserve thin borders and black replacements.
    tiles = roi.reshape(73, 8, 112, 8, 3)
    return np.stack(
        (tiles.mean(axis=(1, 3)), tiles.min(axis=(1, 3)), tiles.max(axis=(1, 3)))
    )


class Detector:
    """Freeze a baseline noise envelope and require controls, identity, and continuous evidence."""

    def __init__(self, nonce: int) -> None:
        self.nonce = nonce
        self.lower: NDArray[np.float64] | None = None
        self.upper: NDArray[np.float64] | None = None
        self.trained = 0
        self.frozen = False
        self.observations: dict[int, Observation] = {}
        self.errors: list[str] = []
        self.bad_frames = 0
        self.setup_frames = 0
        self.started = False
        self.last_time = -1.0
        self.last_tick: int | None = None
        self.tick_changed_at = 0.0
        self.examples: dict[int, Frame] = {}

    def feed(self, frame: Frame, timestamp: float) -> None:
        """Inspect one decoded frame, retaining evidence gaps instead of skipping them."""
        if not np.isfinite(timestamp) or timestamp <= self.last_time:
            self.errors.append("Nonmonotonic or invalid media timestamps.")
            return
        self.last_time = timestamp
        try:
            normalized = normalize(frame)
            nonce, phase, tick = identity(normalized)
            if nonce != self.nonce:
                raise ValueError("Wrong capture source nonce.")
        except ValueError as error:
            if not self.started:
                self.setup_frames += 1
                return
            self.bad_frames += 1
            if str(error) not in self.errors:
                self.errors.append(str(error))
            return
        if not self.started:
            if phase != 0:
                self.setup_frames += 1
                return
            self.started = True
        if tick != self.last_tick:
            self.last_tick = tick
            self.tick_changed_at = timestamp
        elif timestamp - self.tick_changed_at > 1.0:
            self.errors.append("Frozen source counter.")
        observation = self.observations.setdefault(phase, Observation())
        observation.frames += 1
        observation.ticks.add(tick)
        if observation.last is not None:
            observation.largest_gap = max(
                observation.largest_gap, timestamp - observation.last
            )
        observation.first = (
            timestamp if observation.first is None else observation.first
        )
        observation.last = timestamp
        if phase == 0:
            if self.frozen:
                self.errors.append("Baseline changed after calibration was frozen.")
            elif self.trained < 240:
                self.train(normalized)
            return
        if not self.frozen:
            if self.trained < 10:
                self.errors.append("Insufficient training baseline.")
                return
            self.frozen = True
        assert self.lower is not None and self.upper is not None
        value = features(normalized)
        score = (
            float(np.max(np.maximum(self.lower - value, value - self.upper)))
            - NOISE_MARGIN
        )
        if score > 0:
            observation.changed += 1
            if score > observation.maximum:
                observation.maximum = score
                self.examples[phase] = normalized

    def train(self, normalized: Frame) -> None:
        """Widen the per-tile bounds by one baseline frame and its small shifts.

        Marker localization in compressed video drifts by a pixel or two, which moves
        checker edges across tile boundaries; shifted baselines keep that drift quiet
        while interior tiles stay sensitive.

        Args:
            normalized (Frame): A normalized phase-0 baseline frame.
        """
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                value = features(np.roll(normalized, (dy, dx), axis=(0, 1)))
                if self.lower is None or self.upper is None:
                    self.lower, self.upper = value, value.copy()
                else:
                    np.minimum(self.lower, value, out=self.lower)
                    np.maximum(self.upper, value, out=self.upper)
        self.trained += 1

    def health(self, phases: Iterable[int], duration: float) -> list[str]:
        """Require complete calibration and live observations for every requested phase."""
        if not self.started:
            return [
                "The capture never showed the calibrated fixture: blank or black output, the wrong source, or a capture method this GPU setup does not support."
            ]
        errors = list(dict.fromkeys(self.errors))
        for phase in (0, 1, *CONTROL_PHASES, 6, *phases):
            observed = self.observations.get(phase)
            if observed is None or observed.first is None or observed.last is None:
                errors.append(f"Missing phase {phase}.")
                continue
            minimum = 1.0 if phase in CONTROL_PHASES else max(1.0, duration - 0.5)
            if (
                observed.frames < 10
                or len(observed.ticks) < 5
                or observed.last - observed.first < minimum
                or observed.largest_gap > 0.5
            ):
                errors.append(
                    f"Insufficient duration/cadence/liveness in phase {phase}."
                )
            if phase in CONTROL_PHASES and not observed.changed:
                errors.append(f"Positive control {phase} was not detected.")
            if phase in (1, 6) and observed.changed:
                errors.append(
                    f"Held-out baseline {phase} differs; calibration is ambiguous."
                )
        return errors

    def summary(self) -> dict[str, Any]:
        """Export measured cadence and noise sensitivity without raw array payloads."""
        return {
            "errors": list(dict.fromkeys(self.errors)),
            "bad_frames": self.bad_frames,
            "setup_frames": self.setup_frames,
            "noise_ceiling": float(np.max(self.upper - self.lower))
            if self.upper is not None and self.lower is not None
            else None,
            "phases": {
                str(phase): {
                    "frames": item.frames,
                    "changed": item.changed,
                    "maximum": item.maximum,
                    "ticks": len(item.ticks),
                    "first": item.first,
                    "last": item.last,
                    "largest_gap": item.largest_gap,
                }
                for phase, item in self.observations.items()
            },
        }


def synthetic_frame(nonce: int, phase: int, tick: int) -> Frame:
    """Render the same fiducials and header as the browser for detector self-tests."""
    ys, xs = np.indices((HEIGHT, WIDTH))
    gray = np.where((xs // 32 + ys // 32) % 2, 195, 65).astype(np.uint8)
    frame = np.repeat(gray[:, :, None], 3, axis=2)
    frame[:96] = 32
    for (x, y), color in zip(
        ((8, 8), (WIDTH - 24, 8), (8, HEIGHT - 24), (WIDTH - 24, HEIGHT - 24)), COLORS
    ):
        frame[y : y + 16, x : x + 16] = color
    for y, value in ((36, nonce), (56, (phase << 24) | tick)):
        for bit in range(32):
            frame[y : y + 12, 64 + bit * 12 : 76 + bit * 12] = (
                255 if (value >> (31 - bit)) & 1 else 0
            )
    return frame


def synthetic_sequence(
    nonce: int, phases: Iterable[int]
) -> Iterator[tuple[Frame, float]]:
    """Build detector fixtures with visible controls and a continuously advancing counter."""
    tick = 0
    for phase in phases:
        for _ in range(32):
            frame = synthetic_frame(nonce, phase, tick)
            if phase == 2:
                frame[160:520, 200:700] = 0
            elif phase == 3:
                frame[160:520, 200:700] = (frame[160:520, 200:700] * 0.85).astype(
                    np.uint8
                )
            elif phase == 4:
                frame[160:162, 200:700] = 255
            elif phase == 5:
                frame[240:250, 300:500] = 255
            yield frame, tick / 10
            tick += 1
