"""Define the capture matrix and interfaces without importing optional desktop tools."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from stealth.results import Result

SURFACES = (
    "startup",
    "main",
    "hide-show",
    "move-resize",
    "click-through",
    "settings",
    "history",
    "screenshot-tray",
    "screenshot-viewer",
    "transcript",
    "model-popup",
    "effort-popup",
    "custom-model-popup",
    "custom-effort-popup",
    "tooltip",
    "context-menu",
    "file-dialog",
    "directory-dialog",
    "tray-menu",
    "provider-error",
    "recording-error",
    "streaming",
    "handle-recreation",
    "shutdown",
)
APPLICATIONS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "zoom-desktop": (
        "desktop",
        ("monitor", "window"),
        ("auto", "legacy", "filtered", "advanced-filtered", "advanced", "secure"),
    ),
    "zoom-web": ("browser", ("monitor", "window", "tab"), ("default",)),
    "meet-chrome": ("browser", ("monitor", "window", "tab"), ("default",)),
    "meet-edge": ("browser", ("monitor", "window", "tab"), ("default",)),
    "teams-desktop": ("desktop", ("monitor", "window"), ("default",)),
    "teams-chrome": ("browser", ("monitor", "window", "tab"), ("default",)),
    "teams-edge": ("browser", ("monitor", "window", "tab"), ("default",)),
    "discord-desktop": ("desktop", ("monitor", "window"), ("default",)),
    "discord-web": ("browser", ("monitor", "window", "tab"), ("default",)),
    "webex-desktop": ("desktop", ("monitor", "window"), ("default",)),
    "webex-web": ("browser", ("monitor", "window", "tab"), ("default",)),
    "obs": ("obs", ("monitor", "window"), ("auto", "wgc", "dxgi", "bitblt")),
    "snipping-tool": ("recorder", ("monitor",), ("region",)),
    "game-bar": ("recorder", ("window",), ("default",)),
    "chrome-local": ("local", ("monitor", "window", "tab"), ("default",)),
    "edge-local": ("local", ("monitor", "window", "tab"), ("default",)),
}
DIAGNOSTICS = (
    "picker",
    "shell",
    "audio",
    "focus",
    "share-restart",
    "source-change",
    "monitor-transition",
    "local-recording",
)


@dataclass(frozen=True)
class Cell:
    """Identify a required app, source, capture method, display, and repetition."""

    app: str
    source: str
    mode: str
    monitor: int
    repeat: int

    @property
    def id(self) -> str:
        """Return a portable identifier for reports and artifact directories."""
        return (
            f"{self.app}.{self.source}.{self.mode}.display-{self.monitor}.{self.repeat}"
        )


@dataclass
class Config:
    """Keep private setup separate from exported observations."""

    apps: dict[str, dict[str, Any]]
    monitors: tuple[int, ...] = ()
    repeats: int = 3
    duration: float = 3.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    target: str = "source"

    @classmethod
    def read(cls, path: Path) -> Config:
        """Read and validate a local configuration without accepting executable scripts."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise ValueError("Capture config must be a schema 1 object.")
        apps = raw.get("apps", {})
        if not isinstance(apps, dict) or any(
            key not in APPLICATIONS or not isinstance(value, dict)
            for key, value in apps.items()
        ):
            raise ValueError("Unknown application or invalid application settings.")
        monitors = raw.get("monitors", [])
        if (
            not isinstance(monitors, list)
            or any(type(item) is not int or item < 0 for item in monitors)
            or len(set(monitors)) != len(monitors)
        ):
            raise ValueError("Monitors must contain distinct nonnegative indices.")
        repeats = raw.get("repeats", 3)
        duration = raw.get("duration", 3.0)
        if type(repeats) is not int or not 1 <= repeats <= 10:
            raise ValueError(
                "Repeats must be between 1 and 10; release coverage requires three."
            )
        if not isinstance(duration, (int, float)) or not 2 <= duration <= 60:
            raise ValueError("Duration must be between 2 and 60 seconds.")
        return cls(
            apps,
            tuple(monitors),
            repeats,
            float(duration),
            str(raw.get("ffmpeg", "ffmpeg")),
            str(raw.get("ffprobe", "ffprobe")),
            str(raw.get("target", "source")),
        )

    def fingerprint(self) -> str:
        """Bind resume to the complete configuration without exporting account information."""
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


@dataclass
class Capture:
    """Return recorded media and observations from an application adapter."""

    path: Path
    origin: str
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[Result] = field(default_factory=list)


class Adapter(Protocol):
    """Implement one real capture path with bounded acquisition and owned cleanup."""

    def probe(self) -> list[str]:
        """Return missing prerequisites without launching a call."""
        ...

    def start(self, cell: Cell, directory: Path, fixture_url: str) -> None:
        """Join or configure the selected capture and begin retaining actual output."""
        ...

    def pump(self, seconds: float) -> None:
        """Service recorder callbacks while the fixture performs an action."""
        ...

    def finish(self) -> Capture:
        """Finalize and return the receiver or recorder media."""
        ...

    def operate(self, name: str) -> bool:
        """Execute a configured live transition and return whether its recipe exists."""
        ...

    def close(self) -> None:
        """Restore changed settings and close only owned resources."""
        ...


def matrix(apps: list[str], monitors: tuple[int, ...], repeats: int) -> list[Cell]:
    """Expand meaningful capture modes without inventing unsupported source combinations."""
    result = []
    for app in apps:
        _, sources, modes = APPLICATIONS[app]
        for source in sources:
            for mode in modes:
                if app == "obs" and (
                    (source == "monitor" and mode == "bitblt")
                    or (source == "window" and mode == "dxgi")
                ):
                    continue
                for monitor in monitors:
                    for repeat in range(1, repeats + 1):
                        result.append(Cell(app, source, mode, monitor, repeat))
    return result
