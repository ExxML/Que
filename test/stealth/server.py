"""Serve a loopback-only observer and durably receive browser-only evidence."""

import base64
import hashlib
import itertools
import json
import math
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PyQt6.QtGui import QImage

from stealth.manifest import HOTKEYS
from stealth.results import Result, assess_trial, atomic_json, read_json


class EvidenceStore:
    """Keep append-only browser events separate from private native diagnostics."""

    def __init__(self, directory: Path, trials: list[dict[str, Any]]) -> None:
        self.directory = directory
        self.trials = {trial["id"]: trial for trial in trials}
        self.lock = threading.RLock()
        self.redactions: tuple[str, ...] = ()
        self.credential_mode = "unknown"
        self.event_hashes: dict[str, dict[int, str]] = {}
        self.event_times: dict[str, float] = {}
        path = directory / "checkpoint.json"
        self.state: dict[str, Any] = (
            read_json(path)
            if path.exists()
            else {"attempts": {}, "sessions": {}, "errors": []}
        )
        for path in (directory / "browser").glob("*.jsonl"):
            expected = 1
            checkpoint = self.state["sessions"].get(path.stem, {})
            chain = ""
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event["seq"] != expected:
                        raise ValueError("Noncontiguous persisted timeline.")
                    self.event_hashes.setdefault(path.stem, {})[event["seq"]] = (
                        self._event_hash(event)
                    )
                    self.event_times[path.stem] = event["wall_ms"]
                    chain = hashlib.sha256(
                        (chain + self._event_hash(event)).encode()
                    ).hexdigest()
                    if (
                        event["seq"] == checkpoint.get("seq")
                        and checkpoint.get("chain")
                        and chain != checkpoint["chain"]
                    ):
                        raise ValueError(
                            "Persisted event chain differs from the checkpoint."
                        )
                    expected += 1
                if expected - 1 < checkpoint.get("seq", 0):
                    raise ValueError(
                        "Persisted timeline is shorter than its acknowledged checkpoint."
                    )
                self.state["sessions"].setdefault(path.stem, {})["seq"] = expected - 1
                self.state["sessions"][path.stem]["chain"] = chain
            except (OSError, ValueError, KeyError):
                self.state["errors"].append(f"Damaged timeline: {path.name}.")
        for attempt in self.state["attempts"].values():
            if not attempt.get("end_ms"):
                attempt["transport_error"] = (
                    "Runner interrupted before interval completion."
                )
        self.save()

    def save(self) -> None:
        """Commit recoverable metadata without claiming unfinished trials passed."""
        if self.redactions:
            serialized = json.dumps(self.state, allow_nan=False)
            for secret in self.redactions:
                if secret:
                    serialized = serialized.replace(secret, "[redacted credential]")
            self.state = json.loads(serialized)
        atomic_json(self.directory / "checkpoint.json", self.state)

    def events(self, session: str, batch: list[dict[str, Any]]) -> int:
        """Accept contiguous events, reject dropped batches, and acknowledge durable writes."""
        uuid.UUID(session)
        if len(batch) > 2000:
            raise ValueError("Oversized event batch.")
        with self.lock:
            record = self.state["sessions"].setdefault(session, {"seq": 0})
            hashes = self.event_hashes.setdefault(session, {})
            for event in batch:
                if event.get("session", session) != session:
                    raise ValueError("Cross-session event.")
                identity = event.get("attempt")
                if identity and (
                    identity not in self.state["attempts"]
                    or self.state["attempts"][identity]["session"] != session
                ):
                    raise ValueError("Unknown or cross-session attempt.")
                if event["seq"] <= record["seq"] and hashes.get(
                    event["seq"]
                ) != self._event_hash(event):
                    self.state["errors"].append(
                        "Duplicate sequence changed its contents."
                    )
                    self.save()
                    raise ValueError("Conflicting duplicate event.")
            new = [event for event in batch if event["seq"] > record["seq"]]
            expected = record["seq"] + 1
            previous_time = self.event_times.get(session, 0.0)
            for event in new:
                if (
                    event["seq"] != expected
                    or not isinstance(event.get("type"), str)
                    or not isinstance(event.get("wall_ms"), (int, float))
                    or not math.isfinite(event["wall_ms"])
                    or event["wall_ms"] < previous_time
                ):
                    self.state["errors"].append(
                        f"Event sequence gap in {session} at {expected}."
                    )
                    self.save()
                    raise ValueError("Noncontiguous event sequence.")
                expected += 1
                previous_time = event["wall_ms"]
            with (self.directory / "browser" / f"{session}.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                for event in new:
                    serialized = json.dumps(
                        {**event, "received_ms": time.time_ns() // 1_000_000},
                        allow_nan=False,
                    )
                    for secret in self.redactions:
                        if secret:
                            serialized = serialized.replace(
                                secret, "[redacted credential]"
                            )
                    handle.write(serialized + "\n")
                    hashes[event["seq"]] = self._event_hash(event)
                    record["chain"] = hashlib.sha256(
                        (record.get("chain", "") + hashes[event["seq"]]).encode()
                    ).hexdigest()
                handle.flush()
                os.fsync(handle.fileno())
            if new:
                record["seq"] = new[-1]["seq"]
                self.event_times[session] = new[-1]["wall_ms"]
            self.save()
            return int(record["seq"])

    def begin(self, value: dict[str, Any]) -> str:
        """Create a distinct attempt so reruns never erase prior failures."""
        with self.lock:
            spec = self.trials[value["id"]]
            if (
                "QPushButton('Show')" in spec["scenario"].get("action", "")
                and self.credential_mode != "dummy"
            ):
                raise ValueError(
                    "Credential reveal requires the runner's dummy-key mode and an application restart."
                )
            session = str(uuid.UUID(value["session"]))
            if not value.get("configuration_confirmed"):
                raise ValueError("Confirm physical configuration before measurement.")
            attempt_id = (
                str(uuid.UUID(value["attempt"]))
                if value.get("attempt")
                else str(uuid.uuid4())
            )
            if attempt_id in self.state["attempts"]:
                raise ValueError("Attempt identifier already exists.")
            if not isinstance(value["start_ms"], (int, float)) or not math.isfinite(
                value["start_ms"]
            ):
                raise ValueError("Invalid interval start.")
            self.state["attempts"][attempt_id] = {
                **spec,
                "session": session,
                "start_ms": value["start_ms"],
                "attempt": attempt_id,
                "configuration_notes": str(value.get("configuration_notes", "")),
                "evidence": [f"browser/{session}.jsonl"],
                "proof": {},
                "leaks": [],
            }
            self.save()
            return attempt_id

    def finish(self, value: dict[str, Any]) -> None:
        """End an interval before review interactions can contaminate its timeline."""
        with self.lock:
            attempt = self.state["attempts"][value["attempt"]]
            if attempt.get("end_ms"):
                raise ValueError("Measurement interval already ended.")
            if (
                not isinstance(value["end_ms"], (int, float))
                or not math.isfinite(value["end_ms"])
                or value["end_ms"] <= attempt["start_ms"]
            ):
                raise ValueError("Invalid interval end.")
            attempt["end_ms"] = value["end_ms"]
            attempt["final_seq"] = value["final_seq"]
            if (
                self.state["sessions"].get(attempt["session"], {}).get("seq", 0)
                < value["final_seq"]
            ):
                attempt["transport_error"] = "Unacknowledged events at interval end."
            self.save()

    def review(self, value: dict[str, Any]) -> None:
        """Attach specific action evidence and explicit findings after measurement."""
        with self.lock:
            attempt = self.state["attempts"][value["attempt"]]
            if not attempt.get("end_ms"):
                raise ValueError("End the interval before review.")
            for key in ("completion", "review", "unsupported"):
                attempt[key] = str(value.get(key) or "")
            proof = value.get("proof") or {}
            leaks = value.get("leaks") or []
            if (
                not isinstance(proof, dict)
                or not all(isinstance(item, str) for item in proof.values())
                or not isinstance(leaks, list)
                or not all(isinstance(item, str) for item in leaks)
            ):
                raise ValueError("Proof and findings must contain concrete text.")
            attempt["proof"] = proof
            attempt["leaks"] = list(dict.fromkeys([*attempt.get("leaks", []), *leaks]))
            self.save()

    def results(self) -> list[Result]:
        """Evaluate attempts and retain required unperformed matrix entries."""
        with self.lock:
            results = []
            attempt_results: dict[str, list[Result]] = {}
            attempted = set()
            for attempt in self.state["attempts"].values():
                attempted.add(attempt["id"])
                path = self.directory / "browser" / f"{attempt['session']}.jsonl"
                events = []
                session_events = []
                try:
                    expected_sequence = 1
                    for line in path.read_text(encoding="utf-8").splitlines():
                        event = json.loads(line)
                        if event["seq"] != expected_sequence or self.event_hashes.get(
                            attempt["session"], {}
                        ).get(event["seq"]) != self._event_hash(event):
                            attempt["transport_error"] = (
                                "Persisted browser evidence changed or lost events."
                            )
                        expected_sequence += 1
                        session_events.append(event)
                        if event.get("attempt") == attempt["attempt"]:
                            events.append(event)
                    if expected_sequence - 1 < attempt.get("final_seq", 0):
                        attempt["transport_error"] = (
                            "Persisted evidence was truncated after acknowledgement."
                        )
                except (OSError, ValueError):
                    attempt["transport_error"] = "Unreadable browser timeline."
                measured = [
                    event
                    for event in events
                    if attempt["start_ms"]
                    <= event["wall_ms"]
                    <= attempt.get("end_ms", float("inf"))
                ]
                leaks = list(attempt.get("leaks") or [])
                identity = attempt["scenario"]["id"]
                direct_exposure = False
                inactive_chord = (
                    identity.startswith("key.")
                    and identity.endswith(".hidden")
                    and not next(
                        always
                        for _, _, callback, _, always in HOTKEYS
                        if callback == identity.split(".")[1]
                    )
                )
                forbidden_effects = {"blur", "fullscreen-exit", "pointerlock-lost"}
                if any(event["type"] in forbidden_effects for event in measured):
                    leaks.append(
                        "Action interval includes focus loss, fullscreen exit, or pointer-lock loss; review as an observable effect."
                    )
                if any(
                    event["type"] in ("focus-poll", "heartbeat")
                    and event.get("data", {}).get("hasFocus") is False
                    for event in measured
                ):
                    leaks.append(
                        "Browser focus polling observed loss during the action."
                    )
                if any(
                    event["type"] == "audio-quantum"
                    and event.get("data", {}).get("gaps", 0) > 0
                    for event in measured
                ):
                    leaks.append(
                        "Audio worklet observed discontinuous rendering frames."
                    )
                if any(
                    event["type"] == "clipboard-read"
                    and (
                        event.get("data", {}).get("text")
                        or event.get("data", {}).get("bytes", 0) > 0
                    )
                    for event in measured
                ):
                    leaks.append(
                        "Granted clipboard access exposed copied content or formats: permission-boundary limitation."
                    )
                    direct_exposure = True
                if any(
                    event["type"] == "directory-read"
                    and (
                        event.get("data", {}).get("text")
                        or event.get("data", {}).get("name")
                    )
                    for event in measured
                ):
                    leaks.append(
                        "Granted directory access exposed file paths, metadata or content: permission-boundary limitation."
                    )
                    direct_exposure = True
                if identity.startswith("key.") and not inactive_chord:
                    name = identity.split(".")[1]
                    vk = next(
                        key for _, key, callback, _, _ in HOTKEYS if callback == name
                    )
                    code = {
                        37: "ArrowLeft",
                        38: "ArrowUp",
                        39: "ArrowRight",
                        40: "ArrowDown",
                    }.get(vk, f"Key{chr(vk)}")
                    if any(
                        event["type"] in ("keydown", "keyup", "keypress")
                        and event.get("data", {}).get("code")
                        in {
                            code,
                            "ControlLeft",
                            "ControlRight",
                            "AltLeft",
                            "AltRight",
                            "ShiftLeft",
                            "ShiftRight",
                        }
                        for event in measured
                    ):
                        leaks.append(
                            "Suppressed action key or chord modifier reached the browser."
                        )
                        direct_exposure = True
                if any(
                    event["type"] == "capture-difference"
                    and event.get("data", {}).get("calibrated")
                    and (
                        event["data"].get("changed_pixels", 0) > 0
                        or event["data"].get("changed_fraction", 0) > 0
                    )
                    for event in measured
                ):
                    leaks.append("Calibrated capture contains changed/occluded pixels.")
                if any(
                    event["type"] in ("audio-mute", "audio-ended") for event in measured
                ):
                    leaks.append(
                        "Browser audio track was muted or ended during the action."
                    )
                reviewed = {**attempt, "leaks": leaks}
                proof = dict(attempt.get("proof") or {})
                required = attempt["scenario"]["proof"]
                if "frames" in required:
                    frames = [
                        event for event in measured if event["type"] == "capture-frame"
                    ]
                    calibrated_frames = [
                        event
                        for event in measured
                        if event["type"] == "capture-difference"
                        and event.get("data", {}).get("calibrated")
                    ]
                    if len(frames) < 3 or not calibrated_frames:
                        proof.pop("frames", None)
                        proof.pop("positive_control", None)
                    frame_times = [
                        attempt["start_ms"],
                        *[event["wall_ms"] for event in frames],
                        attempt.get("end_ms", attempt["start_ms"]),
                    ]
                    if any(
                        end - start > 1000
                        for start, end in itertools.pairwise(frame_times)
                    ) or any(
                        event["type"] in ("capture-ended", "capture-mute")
                        for event in measured
                    ):
                        proof.pop("frames", None)
                    if frames and any(
                        event.get("data", {}).get("beacon_age_ms", 99999) > 3500
                        for event in frames
                    ):
                        reviewed["transport_error"] = (
                            "Capture is frozen or has the wrong source/beacon mapping."
                        )
                if (
                    "browser_audio" in required
                    and sum(event["type"] == "audio-sample" for event in measured) < 3
                ):
                    proof.pop("browser_audio", None)
                audio_samples = [
                    event for event in measured if event["type"] == "audio-sample"
                ]
                if "browser_audio" in required and audio_samples:
                    audio_times = [
                        attempt["start_ms"],
                        *[event["wall_ms"] for event in audio_samples],
                        attempt.get("end_ms", attempt["start_ms"]),
                    ]
                    if any(
                        end - start > 1500
                        for start, end in itertools.pairwise(audio_times)
                    ) or any(
                        event.get("data", {}).get("state") != "running"
                        for event in audio_samples
                    ):
                        proof.pop("browser_audio", None)
                reviewed["proof"] = proof
                result = assess_trial(
                    reviewed,
                    measured,
                    attempt["scenario"]["proof"],
                    self.directory,
                    session_events,
                )
                result.kind = (
                    "direct exposure"
                    if direct_exposure
                    else "observable effect"
                    if leaks
                    else "browser protection"
                )
                result.check += f"|attempt={attempt['attempt']}"
                results.append(result)
                attempt_results.setdefault(attempt["id"], []).append(result)
            for history in attempt_results.values():
                if history[-1].status in ("PASS", "FAIL"):
                    for historical in history[:-1]:
                        if historical.status in ("INCONCLUSIVE", "UNSUPPORTED"):
                            historical.required = False
                            historical.reason += " Superseded by a completed retry; retained as historical evidence."
            for identity, trial in self.trials.items():
                if identity not in attempted:
                    results.append(
                        Result(
                            identity,
                            "INCONCLUSIVE",
                            "Required desktop trial has not been performed.",
                            "browser",
                            trial["configuration"],
                        )
                    )
            for error in self.state["errors"]:
                results.append(
                    Result("observer.transport", "INCONCLUSIVE", error, "browser")
                )
            return results

    def _event_hash(self, event: dict[str, Any]) -> str:
        """Hash canonical event contents to detect conflicting retries after recovery."""
        serialized = json.dumps(
            {key: value for key, value in event.items() if key != "received_ms"},
            sort_keys=True,
            allow_nan=False,
        )
        for secret in self.redactions:
            if secret:
                serialized = serialized.replace(secret, "[redacted credential]")
        return hashlib.sha256(serialized.encode()).hexdigest()


class ObserverServer(ThreadingHTTPServer):
    """Bind one authenticated loopback origin with no native-data read endpoint."""

    daemon_threads = True

    def __init__(
        self,
        evidence_store: EvidenceStore,
        assets: Path,
        manifest: dict[str, Any],
        isolated: bool = False,
    ) -> None:
        self.store = evidence_store
        self.assets = assets
        self.manifest = manifest
        self.isolated = isolated
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), ObserverHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class ObserverHandler(BaseHTTPRequestHandler):
    """Expose only explicitly allowlisted public assets and browser evidence ingestion."""

    server: ObserverServer

    def handle(self) -> None:
        """Record interrupted clients without trying to write another response to a closed socket."""
        try:
            super().handle()
        except ConnectionError:
            with self.server.store.lock:
                for attempt in self.server.store.state["attempts"].values():
                    if not attempt.get("end_ms"):
                        attempt["transport_error"] = (
                            "Client disconnected before evidence acknowledgement."
                        )
                self.server.store.save()

    def do_GET(self) -> None:
        """Serve static observer assets or authenticated scenario definitions."""
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        assets = {
            "/": ("web-detection-test.html", "text/html"),
            "/observer.js": ("stealth/observer.js", "text/javascript"),
            "/worker.js": ("stealth/worker.js", "text/javascript"),
            "/sw.js": ("stealth/sw.js", "text/javascript"),
            "/research.js": ("stealth/research.js", "text/javascript"),
            "/audio-worklet.js": ("stealth/audio-worklet.js", "text/javascript"),
            "/storage-worker.js": ("stealth/storage-worker.js", "text/javascript"),
        }
        if path in assets:
            name, mime = assets[path]
            self._send((self.server.assets / name).read_bytes(), mime)
        elif path == "/api/manifest" and self._authorized():
            self._json(self.server.manifest)
        elif path == "/api/ping" and self._authorized():
            self._json(
                {
                    "wall_ms": time.time_ns() // 1_000_000,
                    "control": "owned reachable endpoint",
                }
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        """Validate and durably store event batches, trial review, and sampled PNG frames."""
        if not self._authorized():
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 12 * 1024 * 1024:
                raise ValueError("Invalid payload size.")
            value = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            store = self.server.store
            if path == "/api/events":
                self._json({"ack": store.events(value["session"], value["events"])})
            elif path == "/api/begin":
                self._json({"attempt": store.begin(value)})
            elif path == "/api/finish":
                store.finish(value)
                self._json({"saved": True})
            elif path == "/api/review":
                store.review(value)
                self._json({"saved": True})
            elif path == "/api/frame":
                session = str(uuid.UUID(value["session"]))
                frame = base64.b64decode(value["png"], validate=True)
                if not frame.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("Expected PNG frame.")
                if QImage.fromData(frame, "PNG").isNull():
                    raise ValueError("Undecodable PNG frame.")
                name = f"{session}-{uuid.uuid4()}.png"
                (store.directory / "browser" / name).write_bytes(frame)
                if value.get("attempt"):
                    with store.lock:
                        store.state["attempts"][value["attempt"]]["evidence"].append(
                            f"browser/{name}"
                        )
                        store.save()
                self._json(
                    {
                        "path": f"browser/{name}",
                        "sha256": hashlib.sha256(frame).hexdigest(),
                    }
                )
            else:
                self.send_error(404)
        except ConnectionError:
            raise
        except (KeyError, ValueError, TypeError, OSError):
            self.send_error(400, "Invalid or unwritable evidence; trial cannot pass.")

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress URLs and evidence payloads in console logs."""

    def _authorized(self) -> bool:
        """Reject cross-origin writes, DNS rebinding, and unauthenticated requests."""
        return (
            self.headers.get("Host") == urlsplit(self.server.origin).netloc
            and self.headers.get("Origin", self.server.origin) == self.server.origin
            and secrets.compare_digest(
                self.headers.get("X-Audit-Token", ""), self.server.token
            )
        )

    def _send(self, value: bytes, mime: str) -> None:
        """Send a bounded response without caching private session state."""
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.server.isolated:
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(value)

    def _json(self, value: Any) -> None:
        """Serialize one browser-only protocol response."""
        self._send(json.dumps(value, allow_nan=False).encode(), "application/json")
