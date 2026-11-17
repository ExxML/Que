"""Analyse a guided (manual) stealth run; see test/STEALTH_MANUAL_TESTING.md.

Run with the repository's virtual environment from the repository root:

    python test/stealth/manual.py analyze .stealth-runs/RUN [scenario-substring ...]
    python test/stealth/manual.py timeline .stealth-runs/RUN [scenario-substring ...]
    python test/stealth/manual.py calibrate .stealth-runs/RUN OUTDIR
    python test/stealth/manual.py transients .stealth-runs/RUN OUTDIR scenario-substring ...
    python test/stealth/manual.py windows PID
"""

import argparse
import collections
import ctypes
import ctypes.wintypes
import datetime
import itertools
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mss
import mss.tools
import numpy as np
from PyQt6.QtGui import QImage

# Event types that are pure background noise in a summary
NOISE = frozenset(
    {
        "animation",
        "worker-timing",
        "focus-poll",
        "heartbeat",
        "render-workload",
        "pointermove",
    }
)
FOCUS_TYPES = (
    "blur",
    "focus",
    "visibilitychange",
    "fullscreen-exit",
    "pointerlock-lost",
)
KEY_TYPES = ("keydown", "keyup", "keypress")
FOOTPRINT_MARGIN = 24
GRID = 16


def main() -> int:
    """Run the requested analysis.

    Returns:
        int: The process exit code.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("analyze", "timeline"):
        command = commands.add_parser(name)
        command.add_argument("run", type=Path)
        command.add_argument("only", nargs="*")
    command = commands.add_parser("calibrate")
    command.add_argument("run", type=Path)
    command.add_argument("out", type=Path)
    command = commands.add_parser("transients")
    command.add_argument("run", type=Path)
    command.add_argument("out", type=Path)
    command.add_argument("only", nargs="+")
    command = commands.add_parser("windows")
    command.add_argument("pid", type=int)
    arguments = parser.parse_args()
    if arguments.command == "analyze":
        analyze(arguments.run, arguments.only)
    elif arguments.command == "timeline":
        timeline(arguments.run, arguments.only)
    elif arguments.command == "calibrate":
        calibrate(arguments.run, arguments.out)
    elif arguments.command == "transients":
        transients(arguments.run, arguments.out, arguments.only)
    else:
        windows(arguments.pid)
    return 0


def analyze(run: Path, only: list[str]) -> None:
    """Summarise each attempt: key events, focus, text, capture differences and Que's native windows.

    Args:
        run (Path): The run directory.
        only (list[str]): Scenario-ID substrings to include; empty includes every attempt.
    """
    native = _native_samples(run)
    for number, attempt, events in _attempts(run, only):
        start, end = attempt["start_ms"], attempt.get("end_ms", sys.maxsize)
        keys = [
            (
                event["type"],
                event["data"].get("code"),
                event["data"].get("key"),
                event["data"].get("repeat"),
            )
            for event in events
            if event["type"] in KEY_TYPES
        ]
        focus = [
            (event["type"], _clock(event["wall_ms"]))
            for event in events
            if event["type"] in FOCUS_TYPES
        ]
        lost = sum(
            1
            for event in events
            if event["type"] == "focus-poll" and event["data"].get("hasFocus") is False
        )
        ending = next(
            (event for event in events if event["type"] == "interval-end"), {}
        )
        differences = [
            event["data"] for event in events if event["type"] == "capture-difference"
        ]
        changed = [data for data in differences if data.get("changed_pixels", 0) > 0]
        types = collections.Counter(event["type"] for event in events)
        transitions = []
        previous = None
        for sample in native:
            if start <= sample["wall_ms"] <= end:
                state = tuple(
                    sorted(
                        (w["hwnd"], tuple(w["rect"]), hex(w["affinity"]))
                        for w in sample["windows"]
                    )
                )
                if state != previous:
                    transitions.append((_clock(sample["wall_ms"]), state))
                    previous = state
        print(
            f"=== {number}. {attempt['scenario']['id']} [{attempt['configuration']['capture_source']}] "
            f"{_clock(start)}-{_clock(end) if end < sys.maxsize else 'open'} transport_error={attempt.get('transport_error')}"
        )
        print("  key events:", keys or "none")
        print("  focus events:", focus or "none", "| focus-poll false:", lost)
        print("  text at end:", repr(ending.get("data", {}).get("text")))
        print(
            f"  capture-difference: {len(differences)} changed: {len(changed)} "
            f"max changed px: {max((d['changed_pixels'] for d in differences), default=0)}"
        )
        print(
            "  other event types:",
            {kind: count for kind, count in types.items() if kind not in NOISE},
        )
        print(
            "  Que window transitions:",
            transitions[:8],
            "..." if len(transitions) > 8 else "",
        )


def timeline(run: Path, only: list[str]) -> None:
    """Print each attempt's key, focus and click events with the gap since the previous one.

    Gaps between a modifier and the next key, and auto-repeat counts, are what reveal
    held-back or replayed input; compare them with a baseline attempt where Que is quit.

    Args:
        run (Path): The run directory.
        only (list[str]): Scenario-ID substrings to include; empty includes every attempt.
    """
    for number, attempt, events in _attempts(run, only):
        print(
            f"--- {number}. {attempt['scenario']['id']} {_clock(attempt['start_ms'])}"
        )
        previous = None
        repeats: collections.Counter[str] = collections.Counter()
        for event in events:
            if event["type"] not in (
                *KEY_TYPES,
                "blur",
                "focus",
                "pointerdown",
                "wheel",
            ):
                continue
            data = event["data"]
            if event["type"] == "keydown" and data.get("repeat"):
                repeats[str(data.get("key"))] += 1
                continue
            gap = (
                f"+{event['wall_ms'] - previous:.0f} ms" if previous is not None else ""
            )
            label = data.get("key") or data.get("target") or ""
            print(
                f"  {_clock(event['wall_ms'])} {event['type']:11} {label!s:10} {data.get('code') or '':13} {gap}"
            )
            previous = event["wall_ms"]
        print("  auto-repeat key-downs:", dict(repeats) or "none")


def calibrate(run: Path, out: Path) -> None:
    """Print the footprint ROI around Que and the screen regions that change (beacon candidates).

    Capture with mss honours capture exclusion too, so Que itself never appears in
    the saved screenshot; its rectangle comes from the run's native log.

    Args:
        run (Path): The run directory.
        out (Path): The directory for screen.png.
    """
    out.mkdir(parents=True, exist_ok=True)
    samples = [sample for sample in _native_samples(run) if sample["windows"]]
    if not samples:
        print("No Que window in the native log yet: launch Que first.")
        return
    que = max(samples[-1]["windows"], key=_area)
    left, top, right, bottom = que["rect"]
    frames = []
    with mss.mss() as grabber:
        for _ in range(8):
            shot = grabber.grab(grabber.monitors[1])
            frames.append(np.asarray(shot)[:, :, :3].astype(np.int16))
            time.sleep(0.15)
        mss.tools.to_png(shot.rgb, shot.size, output=str(out / "screen.png"))
    changed = np.zeros(frames[0].shape[:2], bool)
    for first, second in itertools.pairwise(frames):
        changed |= np.abs(first - second).max(axis=2) > 0
    height, width = changed.shape
    x, y = max(0, left - FOOTPRINT_MARGIN), max(0, top - FOOTPRINT_MARGIN)
    footprint_width = min(width, right + FOOTPRINT_MARGIN) - x
    footprint_height = min(height, bottom + FOOTPRINT_MARGIN) - y
    print(
        f"monitor {width}x{height}; Que rect {que['rect']} affinity {hex(que['affinity'])}"
    )
    print(
        f"FOOTPRINT {x},{y},{footprint_width},{footprint_height}  changing pixels inside: "
        f"{int(changed[y : y + footprint_height, x : x + footprint_width].sum())}"
    )
    for x0, y0, x1, y1 in _changing_regions(changed):
        ys, xs = np.nonzero(changed[y0:y1, x0:x1])
        print(
            f"changing region x {x0 + xs.min()}-{x0 + xs.max()} y {y0 + ys.min()}-{y0 + ys.max()} ({len(xs)} px)"
        )
    print(f"Screenshot: {out / 'screen.png'}")


def transients(run: Path, out: Path, only: list[str]) -> None:
    """Compare retained capture frames with the baseline inside every non-main Que window.

    Writes side-by-side crops (baseline | frame) to `out`; view them before calling a
    difference a leak, because the cursor is drawn into the capture.

    Args:
        run (Path): The run directory.
        out (Path): The directory for the crops.
        only (list[str]): Scenario-ID substrings to include.
    """
    out.mkdir(parents=True, exist_ok=True)
    native = _native_samples(run)
    for _number, attempt, _events in _attempts(run, only):
        start, end = attempt["start_ms"], attempt.get("end_ms", sys.maxsize)
        frames, baselines = _saved_frames(run, attempt)
        handles: dict[int, dict[str, Any]] = {}
        for sample in native:
            if (
                not start - 1000 <= sample["wall_ms"] <= end + 1000
                or not sample["windows"]
            ):
                continue
            main_window = max(sample["windows"], key=_area)
            for window in sample["windows"]:
                if window["hwnd"] == main_window["hwnd"]:
                    continue
                handle = handles.setdefault(
                    window["hwnd"],
                    {"first": sample["wall_ms"], "rects": [], "affinity": set()},
                )
                handle["last"] = sample["wall_ms"]
                handle["rects"].append(tuple(window["rect"]))
                handle["affinity"].add(hex(window["affinity"]))
        print(
            f"=== {attempt['scenario']['id']} {attempt['attempt'][:8]} frames retained: {len(frames)}"
        )
        for hwnd, handle in sorted(handles.items(), key=lambda item: item[1]["first"]):
            rects = handle["rects"]
            left, top = min(r[0] for r in rects), min(r[1] for r in rects)
            right, bottom = max(r[2] for r in rects), max(r[3] for r in rects)
            print(
                f"  hwnd {hwnd} affinity {sorted(handle['affinity'])} {_clock(handle['first'])}-{_clock(handle['last'])} "
                f"({handle['last'] - handle['first']} ms) rect {(left, top, right, bottom)}"
            )
            for frame in (
                f
                for f in frames
                if handle["first"] - 100 <= f["captured_ms"] <= handle["last"] + 100
            ):
                baseline = _load_image(run / baselines[frame["calibration_epoch"]])
                image = _load_image(run / frame["path"])
                before, after = (
                    baseline[top:bottom, left:right],
                    image[top:bottom, left:right],
                )
                differing = int(
                    (
                        np.abs(before.astype(int) - after.astype(int)).max(axis=2) > 0
                    ).sum()
                )
                name = f"{hwnd}-{frame['captured_ms']}.png"
                _save_image(np.concatenate([before, after], axis=1), out / name)
                print(
                    f"    frame {_clock(frame['captured_ms'])}: {differing}/{before.shape[0] * before.shape[1]} px differ -> {name}"
                )


def windows(pid: int) -> None:
    """List a process's top-level windows with visibility, rectangle and capture affinity.

    Args:
        pid (int): The process ID.
    """
    user32 = ctypes.WinDLL("user32")
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )

    def visit(hwnd: int, _parameter: int) -> bool:
        """Print one window if the process owns it."""
        owner = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            title = ctypes.create_unicode_buffer(200)
            user32.GetWindowTextW(hwnd, title, 200)
            affinity = ctypes.wintypes.DWORD()
            user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
            print(
                hwnd,
                repr(title.value),
                "visible" if user32.IsWindowVisible(hwnd) else "hidden",
                (rect.left, rect.top, rect.right, rect.bottom),
                hex(affinity.value),
            )
        return True

    user32.EnumWindows(callback_type(visit), 0)


def _attempts(
    run: Path, only: list[str]
) -> Iterator[tuple[int, dict[str, Any], list[dict[str, Any]]]]:
    """Yield each selected attempt in start order with its browser events.

    Args:
        run (Path): The run directory.
        only (list[str]): Scenario-ID substrings to include; empty includes every attempt.

    Returns:
        Iterator[tuple[int, dict[str, Any], list[dict[str, Any]]]]: Number, attempt record and events.
    """
    checkpoint = json.loads((run / "checkpoint.json").read_text(encoding="utf-8"))
    attempts = sorted(
        checkpoint["attempts"].values(), key=lambda attempt: attempt["start_ms"]
    )
    sessions: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        if attempt["session"] not in sessions:
            with (run / "browser" / f"{attempt['session']}.jsonl").open(
                encoding="utf-8"
            ) as timeline_file:
                sessions[attempt["session"]] = [
                    json.loads(line) for line in timeline_file
                ]
    for number, attempt in enumerate(attempts, 1):
        if only and not any(part in attempt["scenario"]["id"] for part in only):
            continue
        start, end = attempt["start_ms"], attempt.get("end_ms", sys.maxsize)
        events = [
            event
            for event in sessions[attempt["session"]]
            if start <= event["wall_ms"] <= end
            and event.get("attempt") in (attempt["attempt"], None)
        ]
        yield number, attempt, sorted(events, key=lambda event: event["wall_ms"])


def _native_samples(run: Path) -> list[dict[str, Any]]:
    """Load the native recorder's window samples.

    Args:
        run (Path): The run directory.

    Returns:
        list[dict[str, Any]]: Samples with "wall_ms" and Que's "windows".
    """
    with (run / "native" / "windows.jsonl").open(encoding="utf-8") as samples:
        return [json.loads(line) for line in samples if '"windows"' in line]


def _saved_frames(
    run: Path, attempt: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return an attempt's retained capture frames and the baseline image per calibration.

    Args:
        run (Path): The run directory.
        attempt (dict[str, Any]): The attempt record.

    Returns:
        tuple[list[dict[str, Any]], dict[str, str]]: Frame records, and baseline paths by calibration epoch.
    """
    frames: list[dict[str, Any]] = []
    baselines: dict[str, str] = {}
    with (run / "browser" / f"{attempt['session']}.jsonl").open(
        encoding="utf-8"
    ) as timeline_file:
        for line in timeline_file:
            if '"saved-frame"' not in line:
                continue
            event = json.loads(line)
            if event["data"]["kind"] == "baseline":
                baselines[event["data"]["calibration_epoch"]] = event["data"]["path"]
            elif event.get("attempt") == attempt["attempt"]:
                frames.append(event["data"])
    return frames, baselines


def _changing_regions(changed: np.ndarray[Any, Any]) -> list[tuple[int, int, int, int]]:
    """Group changed pixels into connected regions on a coarse grid, smallest first.

    Args:
        changed (np.ndarray[Any, Any]): Boolean mask of pixels that changed between screenshots.

    Returns:
        list[tuple[int, int, int, int]]: Region bounds (x0, y0, x1, y1) in pixels.
    """
    height, width = changed.shape
    grid = changed[: height // GRID * GRID, : width // GRID * GRID].reshape(
        height // GRID, GRID, width // GRID, GRID
    )
    cells: np.ndarray[Any, Any] = np.asarray(grid.any(axis=(1, 3)))
    seen = np.zeros_like(cells)
    regions = []
    for start_y, start_x in zip(*np.nonzero(cells), strict=True):
        if seen[start_y, start_x]:
            continue
        seen[start_y, start_x] = True
        stack, members = [(int(start_y), int(start_x))], []
        while stack:
            y, x = stack.pop()
            members.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if (
                    0 <= ny < cells.shape[0]
                    and 0 <= nx < cells.shape[1]
                    and cells[ny, nx]
                    and not seen[ny, nx]
                ):
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        ys, xs = [m[0] for m in members], [m[1] for m in members]
        regions.append(
            (min(xs) * GRID, min(ys) * GRID, (max(xs) + 1) * GRID, (max(ys) + 1) * GRID)
        )
    return sorted(regions, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))


def _load_image(path: Path) -> np.ndarray[Any, Any]:
    """Load a PNG as an RGB array.

    Args:
        path (Path): The image file.

    Returns:
        np.ndarray[Any, Any]: Height × width × 3 array.
    """
    image = QImage(str(path)).convertToFormat(QImage.Format.Format_RGB888)
    bits = image.constBits()
    assert bits is not None
    raw = bits.asstring(image.sizeInBytes())
    rows = np.frombuffer(raw, np.uint8).reshape(image.height(), image.bytesPerLine())
    pixels: np.ndarray[Any, Any] = rows[:, : image.width() * 3].reshape(
        image.height(), image.width(), 3
    )
    return pixels.copy()


def _save_image(array: np.ndarray[Any, Any], path: Path) -> None:
    """Save an RGB array as a PNG.

    Args:
        array (np.ndarray[Any, Any]): Height × width × 3 array.
        path (Path): The output file.
    """
    pixels = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = pixels.shape[:2]
    data = pixels.tobytes()  # QImage borrows this buffer, so keep it alive until saved
    QImage(data, width, height, width * 3, QImage.Format.Format_RGB888).save(str(path))


def _area(window: dict[str, Any]) -> int:
    """Return a native window sample's area in pixels.

    Args:
        window (dict[str, Any]): The window sample with a "rect".

    Returns:
        int: Width times height.
    """
    left, top, right, bottom = window["rect"]
    return int((right - left) * (bottom - top))


def _clock(wall_ms: float) -> str:
    """Format a wall-clock timestamp in milliseconds as local HH:MM:SS.mmm.

    Args:
        wall_ms (float): Milliseconds since the epoch.

    Returns:
        str: The local time of day.
    """
    return datetime.datetime.fromtimestamp(wall_ms / 1000).strftime("%H:%M:%S.%f")[:-3]


if __name__ == "__main__":
    sys.exit(main())
