"""Define explicit action contracts and required desktop configurations."""

import itertools
from dataclasses import asdict, dataclass
from typing import Any

HOTKEYS = [
    (8, 69, "toggle_window_visibility", False, True),
    (12, 82, "toggle_recording", False, True),
    (14, 81, "close_app", False, True),
    (8, 86, "speak_latest", False, True),
    (8, 32, "toggle_speech", False, True),
    (8, 37, "move_window_left", True, False),
    (8, 39, "move_window_right", True, False),
    (8, 38, "move_window_up", True, False),
    (8, 40, "move_window_down", True, False),
    (10, 65, "resize_window_narrower", True, False),
    (10, 68, "resize_window_wider", True, False),
    (10, 87, "resize_window_shorter", True, False),
    (10, 83, "resize_window_taller", True, False),
    (8, 87, "scroll_up", True, False),
    (8, 83, "scroll_down", True, False),
    (8, 70, "screenshot", False, False),
    (12, 88, "toggle_click_through", False, False),
    (8, 78, "new_chat", False, False),
    (8, 72, "toggle_history", False, False),
    (8, 67, "answer", False, False),
    (8, 27, "stop_generation", False, False),
    (8, 68, "dismiss_warnings", False, False),
    *((8, 48 + slot % 10, f"preset_{slot}", False, False) for slot in range(1, 11)),
]
SURFACES = [
    "main overlay",
    "chat",
    "input",
    "screenshot tray",
    "transcript panel",
    "history",
    "settings",
    "custom dropdown",
    "Qt combo popup",
    "Audio device selector popups",
    "tooltip",
    "text context menu",
    "file dialog",
    "directory dialog",
    "Config export picker",
    "Config import picker",
    "Config import review",
    "tray menu",
    "startup",
    "provider error",
    "recording error",
    "Claude subprocess",
]
DISPLAYS = ["single-100", "dual-100-150-secondary-left"]
PLACEMENTS = ["same-monitor", "different-monitors"]
WINDOW_MODES = ["normal", "maximized", "F11", "DOM-fullscreen", "fullscreen-video"]
CAPTURE_SOURCES = ["monitor", "window", "browser", "Que-if-offered"]


@dataclass(frozen=True)
class Scenario:
    """Specify reproducible action, completion, observation, and cleanup requirements."""

    id: str
    action: str
    completion: str
    observations: str
    protection: str
    prerequisites: tuple[str, ...] = ("unlocked Windows 11 desktop", "synthetic data")
    proof: tuple[str, ...] = ("physical_action",)
    cleanup: str = "Restore the baseline, close transient UI, stop media, and create a fresh synthetic conversation."
    family: str = "interaction"


def scenarios(inventory: dict[str, Any]) -> list[Scenario]:
    """Return an explicit coverage contract for every current action and surface."""
    items = []
    for mask, vk, name, repeat, always in HOTKEYS:
        setup = ""
        effect = ""
        if name == "dismiss_warnings":
            setup = "Before measuring, open the overlay with multiple warning rows, including an ongoing warning, then set the visibility for this variant. "
            effect = "When visible, all rows close and the badge can reopen the ongoing warnings. When hidden, show Que after measuring and confirm the warnings are still open. "
        for variant in ("normal", "release-modifier-first", "hidden", "stress"):
            items.append(
                Scenario(
                    f"key.{name}.{variant}",
                    f"{setup}Physically press modifiers mask {mask} (Caps Lock=8 Alt=2 Shift=4) + VK {vk}; {variant}. "
                    "During measurement use only this chord, hold for repeats, then release. Test ordinary text after the interval in input.integrity. Stress: stream, record and capture simultaneously.",
                    f"{effect}Record observed {name} completion; repeat callbacks={repeat}; always active={always}. "
                    "Caps Lock chords are consumed even when a hidden conditional shortcut does not run. Quit requires relaunch.",
                    "keydown/up/keypress, beforeinput/input, text, composition, modifier/repeat state, focus, capture",
                    "No matched chord reaches the page; ordinary input remains intact.",
                    family="keyboard",
                )
            )
    for identity, signature in inventory["controls"].items():
        items.append(
            Scenario(
                f"control.{identity}",
                f"Open the containing panel and operate {signature}; test click-through off and on, tooltip, keyboard navigation and dismissal.",
                "Record the visible state transition and accessible control identity. Use only a dummy credential for reveal controls.",
                "focus, selection, pointer boundaries/capture, wheel, geometry, fullscreen and capture frames",
                "No content exposure; record action-caused focus/pointer/fullscreen effects.",
                proof=("physical_action", "state_transition"),
            )
        )
    for surface in SURFACES:
        items.append(
            Scenario(
                f"capture.{surface}",
                f"Display {surface} over the calibrated checkerboard. Create/destroy it, hide/show, toggle click-through, move, and reopen; include first paint and native-handle replacement.",
                "Confirm physically visible surface and its synthetic identifying text; mark exact action interval and bounding region.",
                "decoded frames, underlying checkerboard, black rectangles, shadows, borders, track metadata/labels",
                "No overlay pixels or obscured underlying content with exclusion enabled. Disabled exclusion is a visible exposure/calibration.",
                proof=(
                    "physical_action",
                    "capture_source",
                    "positive_control",
                    "baseline",
                    "frames",
                    "physical_visibility",
                ),
                family="capture",
            )
        )
    actions = [
        (
            "input.integrity",
            "Type a fixed paragraph, navigate, select, copy/paste, use browser shortcuts, AltGr and each installed IME.",
            "Compare exact resulting text/selection and shortcuts with Que absent.",
            "keyboard",
        ),
        (
            "input.send-button",
            "Use the input action button with a synthetic conversation while the observer page has foreground focus and calibrated capture is running.",
            "Confirm that Send submits the typed draft and that Answer captures the screen and sends the same prompt as Caps Lock+C. Record foreground focus, pointer capture, and captured frames for each action.",
            "interaction",
        ),
        (
            "pointer.locks",
            "Acquire pointer lock and supported keyboard lock; move over Que, click and scroll; toggle click-through during a drag.",
            "Confirm locks and pointer movement; record losses and boundary interruptions.",
            "interaction",
        ),
        (
            "fullscreen.transitions",
            "Enter/exit F11, DOM and video fullscreen repeatedly; show/hide Que and open every panel.",
            "Record each fullscreen transition and physically verify the overlay.",
            "topmost",
        ),
        (
            "topmost.load",
            "Move Que and open panels while recording, streaming and sharing; repeat fullscreen transitions.",
            "Record native overlap samples, occlusion/recovery durations and physical visibility.",
            "topmost",
        ),
        (
            "screenshot.lifecycle",
            "Capture singly and repeatedly, remove attachments, scroll the tray, screenshot-and-send, repeat during recording/streaming/fullscreen/sharing.",
            "Verify new screenshot file hashes and attachment counts before and after each action.",
            "screenshot",
        ),
        (
            "screenshot.failure",
            "In the disposable copy only, make its screenshot destination unavailable and attempt capture; restore it afterward.",
            "Record failure UI and absence of a new attachment; restore fixture permissions.",
            "screenshot",
        ),
        (
            "lifecycle.start-stop",
            "Capture before launching Que; start, hide/show, change settings, quit and repeat launch.",
            "Record process start/exit, first native window, first decoded frame and each visible transition.",
            "capture",
        ),
        (
            "config.transfer",
            "Use Settings Config with a disposable backup while the browser remains foreground and calibrated capture runs.",
            "Record the selected path, the expected settings-only file or reviewed preferences, and the visible completion. Verify an unchecked preference and saved conversation stay intact.",
            "interaction",
        ),
        (
            "audio.browser-first",
            "Start real browser microphone capture, then Que microphone/WASAPI loopback recording; speak and play the synthetic phrases.",
            "Verify both You and Them transcript segments plus browser audio samples and track continuity.",
            "audio",
        ),
        (
            "audio.que-first",
            "Start Que recording, then browser capture. Repeat start/stop while hidden and during streaming and sharing.",
            "Verify both transcript sources and uninterrupted browser audio. During stop/restart, verify the transcript freezes at stop and only new speech is added after restart.",
            "audio",
        ),
        (
            "audio.devices",
            "Repeat silence/speech/playback using speakers then headset; select and refresh Que's microphone and playback devices while sharing and recording; change and disable devices; test unavailable device and shutdown.",
            "Verify saved choices across restart and apply them on the next recording. Record devicechange, focus, acquisition errors, mute/end, both transcripts and intentional silence intervals.",
            "audio",
        ),
        (
            "audio.speech",
            "Use a cold speech cache for download stages and Settings → Voice to start the download. For playback stages with the model installed, press Caps Lock+V and Caps Lock+Space and right-click a reply line while the browser captures audio and Que records. Repeat on a nondefault Speech output device and repeat the keyboard shortcuts while Que is hidden.",
            "Verify missing and downloading notices, inline download results, and idle audio until a new playback request. For playback, verify speech on the selected output, first streamed words, pause/resume, restart from the right-clicked line and session closure. Verify word tint and centered follow on a long wrapped reply. Scroll away during playback and pause/resume to rejoin the spoken line. Adjust Settings → Voice → Volume while reading and confirm silence at 0%. Change Speed and verify the voice and tint stay synchronized on the next reply. With the model downloaded, use Test voice to play one sentence at different speeds and volumes. Confirm the saved settings survive reopening. Record calibrated capture, browser audio continuity, page focus, key and pointer-button events, context menus, and the Them transcript.",
            "audio",
        ),
        (
            "transcription.models",
            "Download the cold model, record/load, repeat cached; use an isolated offline failure for download/transcription errors.",
            "Record downloaded file hashes, successful cold/cached transcript and error UI; test panel selection and submit to both providers.",
            "audio",
        ),
        (
            "network.listeners",
            "Inventory Que/child sockets. From the page probe only a separately reviewed owned test endpoint, with the harness endpoint as a reachable control.",
            "Record permission, endpoint ownership and browser result; never infer system traffic from page Resource Timing.",
            "network",
        ),
        (
            "boundary.clipboard",
            "Copy synthetic Que text, then grant browser clipboard read permission and read it.",
            "Verify the unique synthetic text in the page; record the permission boundary as an exposure.",
            "boundary",
        ),
        (
            "boundary.directory",
            "Grant directory-picker access to the synthetic fixture directory only; enumerate and read its synthetic files.",
            "Verify the known file content in the page; record granted-directory access as an exposure.",
            "boundary",
        ),
        (
            "boundary.camera",
            "Grant camera access with a camera aimed at the physical display containing Que.",
            "Save a camera frame showing the synthetic overlay text if visible; do not use screen capture as a substitute.",
            "boundary",
        ),
        (
            "boundary.microphone",
            "Grant microphone access and physically type/click while Que is visible.",
            "Confirm audible interaction in browser samples; distinguish entitled acoustic observations from identification.",
            "boundary",
        ),
        (
            "permissions.inventory",
            "Feature-detect and review remaining permissioned APIs and supported permission queries.",
            "Record supported, denied and unavailable APIs and their relevance; unavailable is not a successful test.",
            "boundary",
        ),
    ]
    for provider in ("gemini", "claude"):
        actions.append(
            (
                f"provider.{provider}",
                f"Use live {provider}: discover models, check connection, send text/screenshot/documents/transcript, stream, cancel, switch providers, resume history, then shutdown.",
                "Record each completed stage and synthetic response; Gemini includes capability-document fetch, Claude includes child startup/exit and fresh session. Use separate offline fixtures for auth/quota/timeout/connection errors.",
                "network",
            )
        )
    for identity, action, completion, family in actions:
        proof = ["physical_action", "state_transition"]
        if family == "audio":
            proof += [
                "microphone_transcript",
                "loopback_transcript",
                "browser_audio",
                "device_configuration",
            ]
        elif family == "topmost":
            proof += ["native_timeline", "physical_visibility"]
        elif family == "screenshot":
            proof += ["screenshot_files", "attachment_state"]
        elif identity.startswith("provider."):
            proof += ["provider_completion", "live_service"]
        elif family == "capture":
            proof += ["positive_control", "frames", "capture_source", "baseline"]
        items.append(
            Scenario(
                identity,
                action,
                completion,
                "Continuous page timeline, capture/media samples and separately correlated native diagnostics.",
                "Report direct exposure and observable effects; permission boundaries are visible limitations.",
                proof=tuple(proof),
                family=family,
            )
        )
    from stealth.cases import expand_cases

    return expand_cases(items)


def matrix(
    items: list[Scenario], browsers: list[str], isolated: bool = False
) -> list[dict[str, Any]]:
    """Expand required configurations and three deterministic repeats without silent omissions."""
    trials = []
    for browser, display, placement, mode in itertools.product(
        browsers, DISPLAYS, PLACEMENTS, WINDOW_MODES
    ):
        if display == "single-100" and placement == "different-monitors":
            continue
        for item in items:
            if item.family == "statistics":
                continue
            captures = (
                CAPTURE_SOURCES
                if item.family == "capture"
                else ["monitor"]
                if "frames" in item.proof
                else ["none"]
            )
            for source, exclusion, repeat in itertools.product(
                captures,
                (True, False) if item.family == "capture" else (True,),
                range(1, 4),
            ):
                config = {
                    "browser": browser,
                    "display": display,
                    "placement": placement,
                    "window_mode": mode,
                    "capture_source": source,
                    "exclusion": exclusion,
                    "repeat": repeat,
                    "cross_origin_isolated": isolated,
                }
                identity = f"{item.id}|{browser}|{display}|{placement}|{mode}|{source}|{exclusion}|{repeat}"
                trials.append(
                    {"id": identity, "scenario": asdict(item), "configuration": config}
                )
    for item in items:
        if item.family == "statistics":
            browser = item.id.split(".")[1].split("-")[0]
            config = {
                "browser": browser,
                "display": "single-100",
                "placement": "same-monitor",
                "window_mode": "normal",
                "capture_source": "none",
                "exclusion": True,
                "repeat": 1,
                "cross_origin_isolated": isolated,
            }
            identity = f"{item.id}|{browser}|single-100|same-monitor|normal|none|True|1"
            trials.append(
                {"id": identity, "scenario": asdict(item), "configuration": config}
            )
    return trials


def statistical_scenarios(schedule: dict[str, list[list[str]]]) -> list[Scenario]:
    """Expose every preregistered paired interval in its randomized order."""
    items = []
    for experiment, batches in schedule.items():
        for batch_index, orders in enumerate(batches, 1):
            for pair_index, order in enumerate(orders, 1):
                for phase in (
                    ("baseline", "action") if order == "AB" else ("action", "baseline")
                ):
                    items.append(
                        Scenario(
                            f"stat.{experiment}.{batch_index}.{pair_index}.{phase}",
                            f"Batch {batch_index}, pair {pair_index}, order {order}: measure {phase} for {experiment}. "
                            "Baseline is Que absent. Action is the named state (absent is a sham control); arrange the state during setup, then keep it stable for at least 15 seconds. Use the CPU/network buttons only for the unrelated controls.",
                            "Record the actual application state and completed operation, plus any concurrent load and media configuration.",
                            "animation cadence, event-loop and worker delays, owned-endpoint latency, rendering and media continuity",
                            "Estimate reproducible statistical effects and sensitivity; never infer identification from timing alone.",
                            proof=(
                                "physical_action",
                                "state_transition",
                                "physical_configuration",
                            ),
                            family="statistics",
                        )
                    )
    return items
