"""Expand workflows into independently evidenced stages and adversarial combinations."""

import itertools
import json
from dataclasses import replace
from pathlib import Path

from stealth.manifest import Scenario

WORKFLOWS = {
    "input.integrity": (
        "type the fixed paragraph",
        "navigate with arrows/Home/End",
        "select with keyboard and pointer",
        "copy and paste the fixture",
        "exercise browser shortcuts",
        "type with AltGr",
        "compose with each installed IME",
    ),
    "input.send-button": (
        "click Send with a typed draft",
        "click Answer with an empty draft",
    ),
    "screenshot.lifecycle": (
        "capture once",
        "capture rapidly ten times",
        "remove one attachment",
        "scroll the screenshot tray",
        "send with screenshot",
        "capture while recording",
        "capture while streaming",
        "capture while sharing",
        "capture in fullscreen",
    ),
    "lifecycle.start-stop": (
        "launch into an already running capture",
        "hide then show",
        "change settings and restart",
        "quit while idle",
        "quit while streaming",
        "quit while recording",
        "repeat launch and shutdown ten times",
    ),
    "config.transfer": (
        "export a settings backup, including browsing and confirming the file location",
        "open and cancel the import picker",
        "open a different backup and cancel the selection review",
        "import one setting by keyboard or pointer while leaving another unchecked",
        "open an invalid backup and verify the inline error",
    ),
    "audio.devices": (
        "record silence with speakers",
        "record speech with speakers",
        "record output playback with speakers",
        "record with headset",
        "change default devices during capture",
        "disable the active device",
        "start with no input device",
        "quit during capture",
        "open Settings Audio and refresh the available devices while recording and sharing",
        "select nondefault You and Them devices by keyboard and pointer, then verify the next recording and restart",
        "unplug a saved device, refresh, verify the unavailable selection, and return to Windows default",
    ),
    "audio.speech": (
        "download the voice from Settings Voice and confirm playback stays idle",
        "read a pending reply from its first streamed word",
        "catch up with a streaming reply and resume on the next word",
        "pause and resume while reading",
        "restart from the beginning while reading",
        "right-click a reply line and read from it",
        "read on a nondefault Speech output device",
        "read while hidden",
        "read while recording, with Que's voice on the Them device",
        "stop reading by starting a new chat",
        "request speech with a cold voice cache and verify the Settings notice",
        "request speech during the voice download and verify the wait notice",
        "fail the isolated voice download and retry from Settings Voice",
        "adjust speech volume during playback, mute at 0%, and restore full volume at 100%",
        "change speech speed and read a reply at the saved speed",
        "use Test voice at different speeds and volumes on the selected output device",
    ),
    "audio.que-first": (
        "start browser capture after Que",
        "stop Que during transcription, then restart promptly and speak a different phrase",
        "record while hidden",
        "record while streaming",
        "record while screen sharing",
    ),
    "transcription.models": (
        "download an uncached model",
        "load and transcribe with the cold model",
        "load and transcribe with the cached model",
        "fail the isolated download",
        "fail the isolated model load",
        "fail isolated inference",
        "select and clear transcript text",
        "submit transcript to Gemini",
        "submit transcript to Claude",
    ),
    "fullscreen.transitions": (
        "enter F11",
        "exit F11",
        "enter DOM fullscreen",
        "exit DOM fullscreen",
        "enter video fullscreen",
        "exit video fullscreen",
        "open a panel while fullscreen",
        "move Que while fullscreen",
    ),
    "pointer.locks": (
        "retain pointer lock while showing Que",
        "retain keyboard lock while operating Que",
        "drag with browser pointer capture across Que",
        "toggle click-through during a drag",
        "scroll through Que",
        "open and dismiss a Que context menu",
    ),
}
PROVIDER_STAGES = (
    "discover models",
    "check connection",
    "send text",
    "send a screenshot",
    "send a document",
    "send a transcript",
    "receive streaming chunks",
    "cancel before first chunk",
    "cancel midstream",
    "switch away during streaming",
    "resume a saved conversation",
    "shutdown during generation",
    "authentication failure",
    "quota response fixture",
    "connection failure",
    "timeout fixture",
    "malformed stream fixture",
    "provider subprocess exit fixture",
)


def expand_cases(items: list[Scenario]) -> list[Scenario]:
    """Give each action stage and high-risk combination its own completion requirement."""
    expanded = []
    for item in items:
        boundary_roles = {
            "boundary.clipboard": "clipboard_probe",
            "boundary.directory": "directory_probe",
            "boundary.camera": "camera_probe",
            "boundary.microphone": "browser_audio",
        }
        if item.id in boundary_roles:
            item = replace(item, proof=(*item.proof, boundary_roles[item.id]))
        if item.family == "network":
            item = replace(
                item,
                proof=(
                    *item.proof,
                    "service_worker_probe",
                    "resource_timing_probe",
                    "owned_endpoint_probe",
                ),
            )
        stages = WORKFLOWS.get(item.id)
        if item.id.startswith("provider."):
            stages = PROVIDER_STAGES + (
                (
                    "fetch Gemini capability documentation",
                    "capability fetch failure fixture",
                )
                if item.id.endswith("gemini")
                else (
                    "start Claude child process",
                    "terminate Claude child process",
                    "discover with missing CLI fixture",
                )
            )
        elif item.id.startswith("control."):
            stages = (
                "activate with click-through off",
                "attempt activation with click-through on",
                "keyboard navigation",
                "display tooltip",
                "dismiss with Escape",
                "close and reopen its panel",
            )
        elif item.id.startswith("capture."):
            stages = (
                "first creation",
                "destruction",
                "hide and show",
                "move across monitor boundaries",
                "toggle click-through",
                "recreate the native handle",
            )
        if stages:
            for index, stage in enumerate(stages, 1):
                expanded.append(
                    replace(
                        item,
                        id=f"{item.id}.stage-{index}",
                        action=f"Only this stage: {stage}. Context: {item.action}",
                        completion=f"Retain before/after artifacts proving this specific stage: {stage}. {item.completion}",
                    )
                )
        else:
            expanded.append(item)
    probes = (
        (
            "storage",
            "Start the bounded OPFS read timing probe. Register fixture size and whether it shares the physical disk with Que; compare cached/cold loading, screenshot and history writes, downloads and unrelated owned I/O controls. Repeat increasing budgets only within available quota. A cached fixture cannot exclude SSD contention.",
            "storage_probe",
        ),
        (
            "memory",
            "Start the worker memory traversal probe. Compare absent/idle/transcription/rendering states against unrelated CPU and memory controls in randomized batches. Record the 32 MiB working set, timer resolution and hardware; this is a contention probe, not a validated Que fingerprint classifier.",
            "memory_probe",
        ),
        (
            "idle",
            "Enable system idle detection. Wait at least 65 seconds with no OS input, then perform only a suppressed physical chord. Repeat with Que absent, idle, hidden, and ordinary input. Correlate idle-to-active with absence/presence of browser key events.",
            "idle_probe",
        ),
        (
            "pressure",
            "Start CPU pressure observation before randomized absent/idle/transcribing/streaming intervals and an unrelated CPU control. Keep foreground and record coarsening, rate limits and missing samples.",
            "pressure_probe",
        ),
        (
            "eyedropper",
            "Use the screen pixel picker to select the same documented physical coordinate with Que absent, an ordinary visible control, and Que visible. Repeat exclusion on/off and every transient surface. Retain physical selection video; cancellation is incomplete.",
            "eyedropper_probe",
        ),
        (
            "screens",
            "Grant display details and move the browser and Que independently between monitors, change DPI and display topology, then repeat without Que. Record screen/current-screen events and labels.",
            "screen_probe",
        ),
        (
            "worklet",
            "Acquire real microphone and enable the audio worklet. Play the fixture tone/speech, then start/stop Que capture in both orders and during transcription. Compare quantum counts, delivery latency, RMS and output clock with absent/control runs.",
            "worklet_probe",
        ),
        (
            "gpu",
            "Enable GPU timing. Randomize Que absent, moving, streaming and transcribing with unrelated rendering controls; collect both synchronized wall time and the timer extension when available. Context loss and disjoint timestamps invalidate a negative result.",
            "gpu_probe",
        ),
        (
            "clipboard-formats",
            "Copy synthetic text, HTML and an image from each applicable Que surface, then read permitted clipboard formats and hashes. Any exposed content remains a boundary exposure.",
            "clipboard_probe",
        ),
    )
    for name, action, proof in probes:
        expanded.append(
            Scenario(
                f"research.{name}",
                action,
                "Retain action receipts, actual API samples and positive/negative controls in the exact configuration.",
                "API events, permission changes, timing and content; separate observations from identification",
                "No claim beyond measured evidence; unsupported or ineffective probes remain incomplete.",
                proof=(
                    "physical_action",
                    "state_transition",
                    proof,
                    "positive_control",
                ),
            )
        )
    states = (
        "streaming",
        "recording",
        "transcribing",
        "screen-sharing",
        "fullscreen",
        "hidden",
        "click-through",
        "provider-error",
    )
    for left, right in itertools.combinations(states, 2):
        expanded.append(
            Scenario(
                f"combination.{left}.{right}",
                f"Establish {left}, enter {right}, trigger screenshot and a movement/toggle chord, then unwind in reverse order. Repeat the opposite acquisition order in a separate attempt. Keep capture and browser media probes running.",
                "Retain receipts for both states, both entry/exit transitions and the triggered actions; one state alone is incomplete.",
                "Physical keys, focus, pointer capture, video pixels, audio continuity and timing",
                "No exemption for simultaneous operations.",
                proof=(
                    "physical_action",
                    "state_transition",
                    "combination_states",
                    "frames",
                    "browser_audio",
                ),
            )
        )
    for name in (
        "streaming-recording-capture",
        "fullscreen-move-popup",
        "shutdown-recording-generation",
        "provider-switch-cancel-history",
    ):
        expanded.append(
            Scenario(
                f"combination.triple.{name}",
                f"Exercise all three operations in {name}; vary acquisition order and release while a physical chord is held.",
                "Retain three-state and transition receipts plus action artifacts.",
                "All enabled probes and exact transition timestamps",
                "Concurrent transitions receive the same protections.",
                proof=(
                    "physical_action",
                    "state_transition",
                    "combination_states",
                    "frames",
                    "browser_audio",
                ),
            )
        )
    expanded.append(
        Scenario(
            "research.public-origin-network",
            "Serve network-origin-test.html on the permissioned HTTPS origin and run network_origin.py for that origin. Test fetch and WebSocket with the owned endpoint running/stopped, then Que absent/idle/active. Repeat permissions prompt/granted/denied with normal browser security.",
            "Retain exported page JSON, owned endpoint log, exact origin/port and repeated reachable/unreachable controls. A permission or CORS failure cannot establish absence of a Que listener.",
            "Public-origin permission state, response content, connection success and timing",
            "Only explicitly owned targets; localhost-origin behavior is a separate condition.",
            proof=(
                "physical_action",
                "network_origin",
                "positive_control",
                "negative_control",
            ),
        )
    )
    operations = json.loads(
        Path(__file__).with_name("operations.json").read_text(encoding="utf-8")
    )
    for identity, operation in operations.items():
        if operation["category"] not in ("failure", "background"):
            continue
        expanded.append(
            Scenario(
                f"operation.{identity}",
                f"Exercise {operation['category']} at src/{operation['source']}:{operation['line']}: {operation['expression']}. Use an isolated fixture for the precise failed dependency or trigger the scheduled worker/timer through its owning UI action. Retain the triggering inputs and source trace; then observe the corresponding unchanged application path where reproducible.",
                "Show that this exact handler/job ran and completed or remained live as intended; a generic error message or another worker is insufficient. Record fixture versus unchanged desktop scope explicitly.",
                "Browser events, error surfaces, capture, media and timing throughout entry, completion and cleanup",
                "Failure and background paths receive the same observation requirements.",
                proof=("physical_action", "state_transition", "operation_trace"),
            )
        )
    ordered = []
    for item in expanded:
        if item.id.startswith("combination.") and not item.id.startswith(
            "combination.triple."
        ):
            _, left, right = item.id.split(".")
            for first, second in ((left, right), (right, left)):
                ordered.append(
                    replace(
                        item,
                        id=f"{item.id}.order-{first}-then-{second}",
                        action=f"Establish {first}, then {second}. Trigger screenshot and a physical movement/toggle chord; unwind {second}, then {first}. Retain both state transitions with capture and browser audio running.",
                    )
                )
        elif item.id.startswith("combination.triple."):
            operations = (
                ("provider-switch", "cancel", "history")
                if item.id.endswith("provider-switch-cancel-history")
                else tuple(item.id.split(".")[-1].split("-"))
            )
            for index, order in enumerate(itertools.permutations(operations), 1):
                ordered.append(
                    replace(
                        item,
                        id=f"{item.id}.order-{index}",
                        action=f"Enter these operations in order: {', '.join(order)}. Release a held physical chord during the last transition, then unwind in reverse order. Retain all three states and their timestamps.",
                    )
                )
        else:
            ordered.append(item)
    return [
        replace(
            item,
            proof=tuple(
                dict.fromkeys(
                    (
                        *item.proof,
                        "physical_configuration",
                        "frames",
                        "capture_source",
                        "positive_control",
                        "baseline",
                    )
                )
            ),
        )
        for item in ordered
    ]
