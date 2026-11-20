"""Acquire real participant output and recorder files through app-specific GUI recipes."""

from __future__ import annotations

import base64
import importlib
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stealth.results import Result, atomic_json

from capture.automation import (
    browser_steps,
    dedicated_profile,
    missing_module,
    native_steps,
)
from capture.contracts import APPLICATIONS, Capture, Cell

FIXTURE_TITLE = "Que capture audit fixture"
TAB_TITLE = f"{FIXTURE_TITLE} - tab"


class MeetingAdapter:
    """Share through a real meeting client and record a distinct browser participant."""

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.settings = settings
        self.contexts: list[Any] = []
        self.playwright: Any = None
        self.receiver: Any = None
        self.sender: Any = None
        self.process: subprocess.Popen[bytes] | None = None
        self.directory = Path()
        self.output = Path()
        self.inventory: list[dict[str, Any]] = []
        self.restores: list[dict[str, Any]] = []
        self.recording = False
        self.local: WindowsRecorderAdapter | None = None
        self.version = ""
        self.source = ""

    def probe(self) -> list[str]:
        """Identify absent accounts, GUI recipes, and automation dependencies."""
        missing = [
            value
            for name in ("playwright", "pywinauto")
            if (value := missing_module(name))
        ]
        for key in (
            "meeting_url",
            "receiver_profile",
            "receiver_join",
            "receiver_selector",
            "share",
            "picker",
        ):
            if not self.settings.get(key):
                missing.append(f"Configure {self.name}.{key} during initial setup.")
        if APPLICATIONS[self.name][0] == "browser":
            for key in ("sender_profile", "sender_join"):
                if not self.settings.get(key):
                    missing.append(f"Configure {self.name}.{key}.")
            if self.settings.get("sender_profile") == self.settings.get(
                "receiver_profile"
            ):
                missing.append("Sender and receiver require distinct test profiles.")
        elif not Path(str(self.settings.get("executable", ""))).is_file():
            missing.append(f"Configure the installed {self.name} executable.")
        if APPLICATIONS[self.name][0] == "desktop":
            for key in ("sender_join", "leave"):
                if not self.settings.get(key):
                    missing.append(f"Configure {self.name}.{key}.")
        for key in ("sender_profile", "receiver_profile"):
            if self.settings.get(key):
                try:
                    dedicated_profile(self.settings[key])
                except ValueError as error:
                    missing.append(str(error))
        return missing

    def start(self, cell: Cell, directory: Path, fixture_url: str) -> None:
        """Join both participants and start recording the actual remote media element."""
        self.directory = directory
        self.source = cell.source
        self.output = directory / "receiver.webm"
        self.output.touch(exist_ok=False)
        self.playwright = (
            importlib.import_module("playwright.sync_api").sync_playwright().start()
        )
        self.receiver = self._browser("receiver")
        self.receiver.goto(self.settings["meeting_url"], wait_until="domcontentloaded")
        browser_steps(self.receiver, self.settings["receiver_join"])
        if APPLICATIONS[self.name][0] == "browser":
            self.sender = self._browser("sender")
            stage = self.sender.context.new_page()
            stage.goto(f"{fixture_url}?tab=1")
            stage.locator("canvas").click()
            self.sender.goto(
                self.settings["meeting_url"], wait_until="domcontentloaded"
            )
            browser_steps(self.sender, self.settings["sender_join"])
        else:
            version_info = importlib.import_module("win32api").GetFileVersionInfo(
                self.settings["executable"], "\\"
            )
            self.version = ".".join(
                str(part)
                for key in ("FileVersionMS", "FileVersionLS")
                for part in (version_info[key] >> 16, version_info[key] & 65535)
            )
            self.process = subprocess.Popen(
                [self.settings["executable"], *self.settings.get("arguments", [])],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            native_steps(self.settings.get("sender_join", []))
        modes = self.settings.get("modes", {})
        if cell.mode not in ("auto", "default"):
            recipe = modes.get(cell.mode)
            if not recipe or not recipe.get("restore"):
                raise RuntimeError(
                    f"Capture mode {cell.mode} needs a set/restore recipe."
                )
            self.restores = recipe["restore"]
            native_steps(recipe["set"])
        share = self.settings["share"].get(cell.source)
        picker = self.settings["picker"].get(cell.source)
        if not share or not picker:
            raise RuntimeError(f"Missing share/picker recipe for {cell.source}.")
        if self.sender is not None:
            browser_steps(self.sender, share)
        else:
            native_steps(share)
        self.inventory = native_steps(picker)
        atomic_json(directory / "picker.json", self.inventory)
        if not self.inventory:
            raise RuntimeError(
                "Picker recipe must retain an inventory before selecting a source."
            )
        browser_steps(self.receiver, self.settings.get("receiver_watch", []))
        self._record(self.settings["receiver_selector"])
        if self.settings.get("local_recording"):
            self.local = WindowsRecorderAdapter(
                self.name, self.settings["local_recording"]
            )
            self.local.start(cell, directory, fixture_url)

    def pump(self, seconds: float) -> None:
        """Dispatch browser callbacks while the recorder streams chunks to disk."""
        self.receiver.wait_for_timeout(seconds * 1000)

    def finish(self) -> Capture:
        """Flush all media chunks before returning evidence and receiver health."""
        metadata = dict(self.receiver.evaluate("() => window.captureAuditStop()"))
        self.recording = False
        if self.local is not None:
            metadata["local_recording"] = self.local.finish().path.name
        metadata["browser"] = (
            str(self.receiver.context.browser.version)
            if self.receiver.context.browser
            else "persistent"
        )
        metadata["sender_version"] = (
            str(self.sender.context.browser.version)
            if self.sender is not None and self.sender.context.browser
            else self.version
        )
        if self.sender is not None:
            sources = [
                source
                for frame in self.sender.frames
                for source in frame.evaluate("() => window.captureAuditSources || []")
            ]
            metadata["sender_sources"] = sources
            expected = "browser" if self.source == "tab" else self.source
            metadata["source_verified"] = (
                bool(sources) and sources[-1].get("surface") == expected
            )
        if not metadata.get("bytes"):
            raise RuntimeError("The receiver delivered no encoded media.")
        diagnostics = []
        names = [
            control["text"] for row in self.inventory for control in row["controls"]
        ]
        leaks = [name for name in names if name.strip().lower() in ("que", "que.exe")]
        diagnostics.append(
            Result(
                "picker",
                "FAIL" if leaks else "INCONCLUSIVE",
                "Que is offered as a share source."
                if leaks
                else "Inventory retained; anonymous entries and thumbnail identity need validation against live windows.",
                kind="capture",
                evidence=["picker.json"],
            )
        )
        return Capture(self.output, "remote-participant", metadata, diagnostics)

    def operate(self, name: str) -> bool:
        """Drive a configured call transition through its actual user interface."""
        recipe = self.settings.get("operations", {}).get(name)
        if not recipe:
            return False
        if self.sender is not None and recipe.get("browser"):
            browser_steps(self.sender, recipe["browser"])
        if recipe.get("native"):
            native_steps(recipe["native"])
        return bool(recipe.get("browser") or recipe.get("native"))

    def close(self) -> None:
        """Stop the recorder, restore explicit mode changes, and close owned browser contexts."""
        errors = []
        if self.recording and self.receiver is not None:
            try:
                self.finish()
            except Exception as error:  # noqa: BLE001  # Continue cleaning up independently owned resources.
                errors.append(type(error).__name__)
        try:
            if self.restores:
                native_steps(self.restores)
            if self.sender is None and self.settings.get("leave"):
                native_steps(self.settings["leave"])
        except Exception as error:  # noqa: BLE001  # Restore other resources even if a vendor window disappeared.
            errors.append(type(error).__name__)
        for context in reversed(self.contexts):
            try:
                context.close()
            except Exception as error:  # noqa: BLE001  # Retain cleanup failures without abandoning other contexts.
                errors.append(type(error).__name__)
        if self.playwright is not None:
            self.playwright.stop()
        if self.local is not None:
            self.local.close()
        if errors:
            raise RuntimeError(f"Capture cleanup needs attention: {', '.join(errors)}.")

    def _browser(self, role: str) -> Any:
        """Open only a dedicated marked profile in an ordinary installed browser."""
        profile = dedicated_profile(self.settings[f"{role}_profile"])
        channel = self.settings.get(
            f"{role}_browser", "msedge" if self.name.endswith("edge") else "chrome"
        )
        context = self.playwright.chromium.launch_persistent_context(
            str(profile),
            channel=channel,
            headless=False,
            no_viewport=True,
            args=[
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
            ]
            if role == "receiver"
            else [],
        )
        self.contexts.append(context)
        if role == "sender":
            context.add_init_script(
                Path(__file__).with_name("sender.js").read_text(encoding="utf-8")
            )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(15000)
        return page

    def _record(self, selector: str) -> None:
        """Start the receiver bridge on the one media element matching the selector."""
        self.receiver.wait_for_function(
            "selector => [...document.querySelectorAll(selector)].some(e => e instanceof HTMLCanvasElement || e.readyState >= 2)",
            arg=selector,
            timeout=30000,
        )
        self.receiver.expose_function("captureAuditChunk", self._chunk)
        script = Path(__file__).with_name("receiver.js").read_text(encoding="utf-8")
        self.receiver.evaluate(script, {"selector": selector, "canvasFPS": 30})
        self.recording = True

    def _chunk(self, encoded: str) -> None:
        """Append bounded, ordered media chunks without accepting file paths from a page."""
        if len(encoded) > 32 * 1024 * 1024:
            raise ValueError("Receiver media chunk exceeded its limit.")
        with self.output.open("ab") as handle:
            handle.write(base64.b64decode(encoded, validate=True))


class LocalShareAdapter(MeetingAdapter):
    """Record Chromium's own screen-share track without a meeting or account.

    Chromium's command-line auto-selection replaces the source picker, so this covers the
    capture stack shared by browser meeting clients but not their WebRTC transport.
    """

    def probe(self) -> list[str]:
        """Require only the browser and Windows automation dependencies."""
        return [
            value
            for name in ("playwright", "pywinauto")
            if (value := missing_module(name))
        ]

    def start(self, cell: Cell, directory: Path, fixture_url: str) -> None:
        """Share the selected screen, fixture window, or fixture tab and record the track."""
        self.directory = directory
        self.source = cell.source
        self.output = directory / "receiver.webm"
        self.output.touch(exist_ok=False)
        win32api = importlib.import_module("win32api")
        monitors = win32api.EnumDisplayMonitors()
        if cell.source == "tab":
            selection = f"--auto-select-tab-capture-source-by-title={TAB_TITLE}"
        elif cell.source == "window":
            selection = f"--auto-select-desktop-capture-source={FIXTURE_TITLE}"
        else:
            # Chromium names a lone display "Entire screen" and numbers several displays.
            name = (
                "Entire screen" if len(monitors) == 1 else f"Screen {cell.monitor + 1}"
            )
            selection = f"--auto-select-desktop-capture-source={name}"
        self.playwright = (
            importlib.import_module("playwright.sync_api").sync_playwright().start()
        )
        browser = self.playwright.chromium.launch(
            channel="msedge" if self.name.startswith("edge") else "chrome",
            headless=False,
            args=[
                selection,
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        self.contexts.append(browser)
        context = browser.new_context(no_viewport=True)
        context.add_init_script(
            Path(__file__).with_name("sender.js").read_text(encoding="utf-8")
        )
        if cell.source == "tab":
            stage = context.new_page()
            stage.goto(f"{fixture_url}?tab=1")
            stage.locator("canvas").click()
        page = context.new_page()
        page.set_default_timeout(15000)
        page.goto(f"{fixture_url}recorder")
        settings = page.evaluate(
            """async surface => {
                const stream = await navigator.mediaDevices.getDisplayMedia({
                    video: {displaySurface: surface, frameRate: 30}, audio: true,
                    systemAudio: "include", selfBrowserSurface: "exclude"});
                const video = document.createElement("video");
                video.id = "capture-audit-share"; video.muted = true; video.srcObject = stream;
                document.body.append(video); await video.play();
                return stream.getVideoTracks()[0].getSettings();
            }""",
            "browser" if cell.source == "tab" else cell.source,
        )
        if cell.source == "monitor":
            left, top, right, bottom = win32api.GetMonitorInfo(
                monitors[cell.monitor][0]
            )["Monitor"]
            # Chromium can report a display one pixel larger than its Win32 bounds.
            if (
                abs(int(settings.get("width", 0)) - (right - left)) > 2
                or abs(int(settings.get("height", 0)) - (bottom - top)) > 2
            ):
                raise RuntimeError(
                    f"{selection} captured a {settings.get('width')}x{settings.get('height')} screen, not display {cell.monitor}."
                )
            # Chromium's floating "is sharing your screen" bar would otherwise sit, and
            # repaint, inside the captured screen.
            bar = (
                importlib.import_module("pywinauto")
                .Desktop(backend="uia")
                .window(title=f"{fixture_url.rstrip('/')} is sharing your screen.")
            )
            bar.wait("visible", timeout=15)
            bar.child_window(title="Hide").wrapper_object().invoke()
            # A hidden bar keeps its window but collapses to an empty rectangle.
            importlib.import_module("pywinauto.timings").wait_until(
                10, 0.2, lambda: bar.rectangle().width() == 0
            )
        # A background recorder tab would throttle its preview element's frame callbacks.
        page.bring_to_front()
        self.sender = self.receiver = page
        self.version = str(browser.version)
        self._record("#capture-audit-share")

    def finish(self) -> Capture:
        """Return the recorded track, keeping preview-element skips as evidence only."""
        capture = super().finish()
        # The element only previews the shared track: MediaRecorder records the track itself,
        # and the detector checks the decoded timestamps of every measured phase.
        capture.metadata["preview_skipped"] = capture.metadata.pop("skipped", 0)
        return capture


class OBSAdapter:
    """Record a temporary OBS scene and restore the user's previous scene and output path."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.client: Any = None
        self.scene = f"Que-capture-audit-{uuid.uuid4().hex}"
        self.previous_scene = ""
        self.previous_directory = ""
        self.recording = False
        self.recorded = False
        self.created = False
        self.directory = Path()

    def probe(self) -> list[str]:
        """Require an explicit local OBS WebSocket connection and source configuration."""
        missing = [value] if (value := missing_module("obsws_python")) else []
        if not self.settings.get("port"):
            missing.append(
                "Enable OBS WebSocket and configure obs.port (normally 4455)."
            )
        if not self.settings.get("sources"):
            missing.append(
                "Configure OBS source settings for each capture method and monitor."
            )
        return missing

    def start(self, cell: Cell, directory: Path, _fixture_url: str) -> None:
        """Create an isolated scene using an explicit capture backend and start recording."""
        self.directory = directory
        password = secret(
            str(self.settings.get("password_env", "QUE_CAPTURE_OBS_PASSWORD"))
        )
        self.client = importlib.import_module("obsws_python").ReqClient(
            host="127.0.0.1",
            port=int(self.settings["port"]),
            password=password,
            timeout=15,
        )
        if (
            self.client.get_record_status().output_active
            or self.client.get_stream_status().output_active
        ):
            raise RuntimeError(
                "OBS is already recording or streaming; leave the existing session untouched."
            )
        if any(
            str(scene["sceneName"]).startswith("Que-capture-audit-")
            for scene in self.client.get_scene_list().scenes
        ):
            # Its saved "previous" settings would be the earlier audit's, not the user's.
            raise RuntimeError(
                "An earlier audit left OBS changed; restore its scene and recording folder first."
            )
        key = f"{cell.source}.{cell.mode}.{cell.monitor}"
        spec = self.settings["sources"].get(key)
        if not spec:
            raise RuntimeError(f"Configure OBS source settings for {key}.")
        self.previous_scene = str(self.client.get_current_program_scene().scene_name)
        self.previous_directory = str(
            self.client.get_record_directory().record_directory
        )
        (directory / "private").mkdir(exist_ok=True)
        atomic_json(
            directory / "private" / "obs-restore.json",
            {"scene": self.previous_scene, "record_directory": self.previous_directory},
        )
        self.client.create_scene(self.scene)
        self.created = True
        # Scenes are sources too, so the input needs a name distinct from its scene.
        item = self.client.create_input(
            self.scene, f"{self.scene}-source", spec["kind"], spec["settings"], True
        ).scene_item_id
        video = self.client.get_video_settings()
        # Fit the whole source inside the canvas so a smaller canvas cannot crop the fixture.
        self.client.set_scene_item_transform(
            self.scene,
            item,
            {
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsWidth": float(video.base_width),
                "boundsHeight": float(video.base_height),
            },
        )
        self.client.set_current_program_scene(self.scene)
        self.client.set_record_directory(str(directory.resolve()))
        self.client.start_record()
        self.recording = self.recorded = True

    def pump(self, seconds: float) -> None:
        """Allow OBS to encode while Que performs its action."""
        time.sleep(seconds)

    def finish(self) -> Capture:
        """Return OBS's finalized recording rather than a preview screenshot."""
        response = self.client.stop_record()
        self.recording = False
        self._wait_stopped()
        path = Path(response.output_path).resolve()
        if path.parent != self.directory.resolve() or not path.is_file():
            raise RuntimeError(
                "OBS returned an unexpected or unfinished recording path."
            )
        return Capture(
            path,
            "obs-recording",
            {
                "version": str(self.client.get_version().obs_version),
                "source_settings": self.client.get_input_settings(
                    f"{self.scene}-source"
                ).input_settings,
            },
        )

    def operate(self, name: str) -> bool:
        """Apply an explicitly configured OBS UI transition without guessing capture modes."""
        recipe = self.settings.get("operations", {}).get(name)
        if recipe:
            native_steps(recipe)
            return True
        return False

    def close(self) -> None:
        """Restore OBS even when recording or analysis fails."""
        if self.client is None:
            return
        steps: list[Callable[[], Any]] = []
        if self.recording:
            steps.append(self.finish)
        if self.recorded:
            # OBS refuses settings changes until the output has fully stopped.
            steps.append(self._wait_stopped)
        if self.previous_directory:
            steps.append(
                lambda: self.client.set_record_directory(self.previous_directory)
            )
        if self.previous_scene:
            steps.append(
                lambda: self.client.set_current_program_scene(self.previous_scene)
            )
        if self.created:
            steps.append(lambda: self.client.remove_scene(self.scene))
        errors = []
        # Each restore runs even when an earlier one fails.
        for step in steps:
            try:
                step()
            except Exception as error:  # noqa: BLE001  # Attempt every restore and report failures.
                errors.append(type(error).__name__)
        self.client.disconnect()
        if errors:
            raise RuntimeError(f"OBS restore needs attention: {', '.join(errors)}.")

    def _wait_stopped(self) -> None:
        """Wait until OBS has finished writing and closed its recording output."""
        deadline = time.monotonic() + 30
        while self.client.get_record_status().output_active:
            if time.monotonic() > deadline:
                raise RuntimeError("OBS did not finish stopping its recording.")
            time.sleep(0.2)


class WindowsRecorderAdapter:
    """Drive Windows recording UI and collect only the new finalized output file."""

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.settings = settings
        self.directory = Path()
        self.before: set[Path] = set()
        self.started = False

    def probe(self) -> list[str]:
        """Require explicit start/stop recipes and the tool's real output directory."""
        missing = [value] if (value := missing_module("pywinauto")) else []
        for key in ("start", "stop", "recordings"):
            if not self.settings.get(key):
                missing.append(f"Configure {self.name}.{key}.")
        return missing

    def start(self, _cell: Cell, directory: Path, _fixture_url: str) -> None:
        """Snapshot existing files and start the selected Windows recorder."""
        self.directory = directory
        self.before = set(Path(self.settings["recordings"]).glob("*.mp4"))
        self.started = True
        native_steps(self.settings["start"])

    def pump(self, seconds: float) -> None:
        """Wait while the actual recording tool collects the fixture."""
        time.sleep(seconds)

    def finish(self) -> Capture:
        """Stop capture and wait for a unique new recording to finish writing."""
        native_steps(self.settings["stop"])
        self.started = False
        deadline = time.monotonic() + 30
        previous: tuple[Path, int] | None = None
        stable = 0
        while time.monotonic() < deadline:
            files = set(Path(self.settings["recordings"]).glob("*.mp4")) - self.before
            if len(files) > 1:
                raise RuntimeError(
                    "Multiple new recordings; refuse to guess which one belongs to this run."
                )
            if files:
                path = next(iter(files))
                current = (path, path.stat().st_size)
                stable = stable + 1 if current == previous and current[1] else 0
                previous = current
                if stable >= 3:
                    output = self.directory / "recording.mp4"
                    shutil.copyfile(path, output)
                    return Capture(output, f"{self.name}-recording")
            time.sleep(1)
        raise RuntimeError("Recording did not finalize within 30 seconds.")

    def close(self) -> None:
        """Stop only the recording started by this adapter."""
        if self.started:
            native_steps(self.settings["stop"])
            self.started = False

    def operate(self, name: str) -> bool:
        """Execute an explicitly configured recorder transition."""
        recipe = self.settings.get("operations", {}).get(name)
        if recipe:
            native_steps(recipe)
            return True
        return False


def create_adapter(
    name: str, settings: dict[str, Any]
) -> MeetingAdapter | OBSAdapter | WindowsRecorderAdapter:
    """Select an adapter from the reviewed application matrix."""
    if APPLICATIONS[name][0] == "local":
        return LocalShareAdapter(name, settings)
    if name == "obs":
        return OBSAdapter(settings)
    if name in ("snipping-tool", "game-bar"):
        return WindowsRecorderAdapter(name, settings)
    return MeetingAdapter(name, settings)


def secret(name: str) -> str:
    """Read one secret from the environment or the repository's ignored .env file.

    The value is never exported, so browsers and fixtures started by the audit cannot inherit it.

    Args:
        name (str): The variable name.

    Returns:
        str: The value, or an empty string when it is not set.
    """
    if name in os.environ:
        return os.environ[name]
    path = Path(__file__).resolve().parents[2] / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip("\"'")
    return ""
