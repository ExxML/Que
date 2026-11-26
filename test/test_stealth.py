"""Verify audit detectors, isolation, recovery, and result rules without live services."""

import hashlib
import json
import tempfile
import unittest
import uuid
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from stealth.analysis import frame_difference, paired_statistics, randomized_schedule
from stealth.experiments import METRICS, analyze_statistics
from stealth.inventory import compare_inventory, discover
from stealth.isolation import child_environment, stage
from stealth.manifest import HOTKEYS, Scenario, matrix, scenarios
from stealth.results import Result, assess_trial, atomic_json, verdict, write_reports
from stealth.server import EvidenceStore, ObserverHandler


def action_receipt(directory: Path, trial: dict[str, Any], roles: list[str]) -> str:
    """Create a synthetic raw action artifact and its hash-bound receipt for detector self-tests."""
    identity = str(trial["attempt"]).replace("|", "-")
    raw = directory / f"{identity}-action.json"
    atomic_json(
        raw,
        {
            "fixture": True,
            "before": {"visible": False},
            "after": {"visible": True},
            "wall_ms": trial["start_ms"] + 500,
        },
    )
    receipt = directory / f"{identity}-receipt.json"
    atomic_json(
        receipt,
        {
            "schema": 1,
            "attempt": trial["attempt"],
            "trial": trial["id"],
            "configuration": trial.get("configuration", {}),
            "roles": [*roles, "completion"],
            "observed_ms": trial["start_ms"] + 500,
            "method": "automated",
            "before": "hidden",
            "after": "visible",
            "reviewer": "synthetic detector self-test",
            "artifacts": [
                {
                    "path": raw.name,
                    "kind": "fixture-trace",
                    "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                }
            ],
        },
    )
    return f"receipt:{receipt.name}#{hashlib.sha256(receipt.read_bytes()).hexdigest()}"


def interval_events(trial: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a complete synthetic interval with durable markers and continuous heartbeats."""
    events = [
        {
            "type": "interval-start",
            "wall_ms": trial["start_ms"],
            "data": {
                "hasFocus": True,
                "visibility": "visible",
                "configuration": trial.get("configuration", {}),
            },
        }
    ]
    events.extend(
        {"type": "heartbeat", "wall_ms": value, "data": {"hasFocus": True}}
        for value in range(trial["start_ms"], trial["end_ms"], 1000)
    )
    events.append({"type": "interval-end", "wall_ms": trial["end_ms"], "data": {}})
    return [
        {**event, "attempt": trial["attempt"], "seq": index}
        for index, event in enumerate(events, 1)
    ]


class ResultTests(unittest.TestCase):
    """Reject missing evidence and keep known failures visible."""

    def setUp(self) -> None:
        """Create one fully evidenced negative observation."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.trial = {
            "id": "fixture",
            "attempt": "fixture",
            "start_ms": 1000,
            "end_ms": 4000,
            "completion": "Observed movement",
            "review": "Reviewed timeline",
            "proof": {"physical_action": "Saw movement"},
        }
        reference = action_receipt(self.path, self.trial, ["physical_action"])
        self.trial["completion"] = reference
        self.trial["proof"] = {"physical_action": reference}
        self.events = interval_events(self.trial)

    def test_negative_observation_requires_completion(self) -> None:
        """Allow a bounded pass only after completion, review and healthy observation."""
        self.assertEqual(
            assess_trial(
                self.trial, self.events, ["physical_action"], self.path
            ).status,
            "PASS",
        )
        for key in ("completion", "review", "end_ms", "proof"):
            with self.subTest(key=key):
                incomplete = {k: v for k, v in self.trial.items() if k != key}
                self.assertEqual(
                    assess_trial(
                        incomplete, self.events, ["physical_action"], self.path
                    ).status,
                    "INCONCLUSIVE",
                )

    def test_dead_observer_and_truncated_recording(self) -> None:
        """Reject dead, overflowing, closed, discontinuous and failed loggers."""
        for fault in ("logger-error", "overflow", "pagehide", "observer-error"):
            self.assertEqual(
                assess_trial(
                    self.trial, self.events + [{"type": fault, "wall_ms": 3500}], []
                ).status,
                "INCONCLUSIVE",
            )
        self.assertEqual(assess_trial(self.trial, [], []).status, "INCONCLUSIVE")
        self.trial["end_ms"] = 12000
        self.assertEqual(
            assess_trial(self.trial, self.events, []).status, "INCONCLUSIVE"
        )

    def test_failures_override_missing_evidence(self) -> None:
        """Retain a direct observed leak even when the rest of a trial is incomplete."""
        self.trial["leaks"] = ["E reached the page"]
        self.assertEqual(assess_trial(self.trial, [], []).status, "FAIL")
        self.assertEqual(verdict([Result("known", "FAIL", "reproduced")]), ("FAIL", 1))

    def test_incomplete_matrix_never_passes(self) -> None:
        """Distinguish successful scope, failure, and incomplete coverage exit codes."""
        for status in ("INCONCLUSIVE", "UNSUPPORTED"):
            self.assertEqual(verdict([Result("missing", status, "missing")])[1], 2)
        self.assertEqual(verdict([])[1], 2)
        self.assertEqual(verdict([Result("measured", "PASS", "bounded")])[1], 0)

    def test_reports_escape_browser_content(self) -> None:
        """Write linked HTML, JSON and JUnit without executing evidence text."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_reports(
                path, {}, [Result("<script>", "FAIL", "<img onerror=alert(1)>")]
            )
            self.assertNotIn("<script>", (path / "report.html").read_text())
            self.assertIn("&lt;script&gt;", (path / "report.html").read_text())
            self.assertIn("failure", (path / "junit.xml").read_text())
            self.assertEqual(
                json.loads((path / "report.json").read_text())["summary"], "FAIL"
            )


class ObserverTests(unittest.TestCase):
    """Inject observable faults into the same evaluator used by desktop runs."""

    def setUp(self) -> None:
        """Create a private durable recorder with one physical-key trial."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        (self.path / "browser").mkdir()
        self.spec = {
            "id": "key.toggle_window_visibility.normal|fixture",
            "configuration": {},
            "scenario": {
                "id": "key.toggle_window_visibility.normal",
                "proof": ["physical_action"],
            },
        }
        self.session = str(uuid.uuid4())
        self.store = EvidenceStore(self.path, [self.spec])
        self.attempt = self.store.begin(
            {
                "id": self.spec["id"],
                "session": self.session,
                "configuration_confirmed": True,
                "start_ms": 1000,
            }
        )

    def submit(self, fault: dict[str, object] | None = None) -> Result:
        """Complete the fixture interval with an optional deliberately observable fault."""
        trial = self.store.state["attempts"][self.attempt]
        events = interval_events({**trial, "end_ms": 4000})
        if fault:
            events.insert(-1, {"wall_ms": 3500, "attempt": self.attempt, **fault})
        for sequence, event in enumerate(events, 1):
            event["seq"] = sequence
        self.store.events(self.session, events)
        self.store.finish(
            {"attempt": self.attempt, "end_ms": 4000, "final_seq": len(events)}
        )
        reference = action_receipt(self.path, trial, ["physical_action"])
        self.store.review(
            {
                "attempt": self.attempt,
                "proof": {"physical_action": reference},
                "completion": reference,
                "review": "Reviewed synthetic detector artifacts",
            }
        )
        return self.store.results()[0]

    def test_valid_negative(self) -> None:
        """Recognize a healthy completed negative control."""
        self.assertEqual(self.submit().status, "PASS")

    def test_leaked_key(self) -> None:
        """Detect a deliberately forwarded suppressed key."""
        self.assertEqual(
            self.submit({"type": "keydown", "data": {"code": "KeyE"}}).status, "FAIL"
        )

    def test_focus_loss(self) -> None:
        """Detect a deliberately introduced focus loss."""
        self.assertEqual(self.submit({"type": "blur"}).status, "FAIL")

    def test_always_active_hidden_shortcut_still_requires_suppression(self) -> None:
        """Keep hidden-window toggle and recording shortcuts covered by the leak detector."""
        self.store.state["attempts"][self.attempt]["scenario"]["id"] = (
            "key.toggle_window_visibility.hidden"
        )
        self.assertEqual(
            self.submit({"type": "keydown", "data": {"code": "KeyE"}}).status, "FAIL"
        )

    def test_inactive_hidden_shortcut_can_pass_through(self) -> None:
        """Allow an ordinary browser action when its Que binding is visibility-dependent."""
        self.store.state["attempts"][self.attempt]["scenario"]["id"] = (
            "key.toggle_history.hidden"
        )
        self.assertEqual(
            self.submit({"type": "keydown", "data": {"code": "KeyH"}}).status, "PASS"
        )

    def test_visible_window(self) -> None:
        """Detect a positive-control calibrated visible native surface."""
        self.assertEqual(
            self.submit(
                {
                    "type": "capture-difference",
                    "data": {"calibrated": True, "changed_fraction": 0.1},
                }
            ).status,
            "FAIL",
        )

    def test_clipboard_exposure(self) -> None:
        """Keep granted clipboard content visible as a boundary exposure."""
        self.assertEqual(
            self.submit(
                {"type": "clipboard-read", "data": {"text": "QUE-AUDIT"}}
            ).status,
            "FAIL",
        )

    def test_audio_disruption(self) -> None:
        """Detect an intentionally interrupted browser audio track."""
        self.assertEqual(self.submit({"type": "audio-ended"}).status, "FAIL")

    def test_incomplete_capture_cannot_be_attested_away(self) -> None:
        """Reject claimed capture proof without actual decoded/calibrated frames."""
        self.store.state["attempts"][self.attempt]["scenario"]["proof"].extend(
            ["frames", "positive_control"]
        )
        self.assertEqual(self.submit().status, "INCONCLUSIVE")

    def test_gap_rejection_and_idempotent_retry(self) -> None:
        """Acknowledge duplicate delivery once and reject dropped events."""
        event = {
            "seq": 1,
            "type": "heartbeat",
            "wall_ms": 1000,
            "attempt": self.attempt,
        }
        self.assertEqual(self.store.events(self.session, [event]), 1)
        self.assertEqual(self.store.events(self.session, [event]), 1)
        self.assertEqual(
            len(
                (self.path / "browser" / f"{self.session}.jsonl")
                .read_text()
                .splitlines()
            ),
            1,
        )
        with self.assertRaises(ValueError):
            self.store.events(self.session, [{**event, "seq": 3}])
        self.assertTrue(self.store.state["errors"])

    def test_resume_marks_interrupted_attempt(self) -> None:
        """Preserve unfinished runs as incomplete after restart."""
        resumed = EvidenceStore(self.path, [self.spec])
        self.assertEqual(resumed.results()[0].status, "INCONCLUSIVE")
        self.assertTrue(resumed.state["attempts"][self.attempt]["transport_error"])

    def test_recover_fsynced_events_after_checkpoint_crash(self) -> None:
        """Recover the durable sequence number when checkpoint update was interrupted."""
        event = {"seq": 1, "type": "heartbeat", "wall_ms": 1000}
        with (self.path / "browser" / f"{self.session}.jsonl").open("w") as handle:
            handle.write(json.dumps(event) + "\n")
        resumed = EvidenceStore(self.path, [self.spec])
        self.assertEqual(resumed.events(self.session, [event]), 1)

    def test_rerun_does_not_erase_failure(self) -> None:
        """Keep failed historical attempts when another attempt starts."""
        self.submit({"type": "blur"})
        self.store.begin(
            {
                "id": self.spec["id"],
                "session": self.session,
                "configuration_confirmed": True,
                "start_ms": 5000,
            }
        )
        self.assertEqual(verdict(self.store.results())[1], 1)

    def test_completed_retry_can_resolve_interrupted_coverage(self) -> None:
        """Retain abandoned attempts while allowing a completed replacement to close the gap."""
        self.store = EvidenceStore(self.path, [self.spec])
        self.attempt = self.store.begin(
            {
                "id": self.spec["id"],
                "session": self.session,
                "configuration_confirmed": True,
                "start_ms": 1000,
            }
        )
        self.submit()
        results = self.store.results()
        self.assertEqual(results[0].status, "INCONCLUSIVE")
        self.assertFalse(results[0].required)
        self.assertEqual(verdict(results)[1], 0)


class CalibrationTests(unittest.TestCase):
    """Test image and statistical detectors with known controls."""

    def test_pixels_detect_visible_black_and_missing_content(self) -> None:
        """Detect overlay colors, black replacement and missing patterned pixels."""
        reference = np.full((30, 40, 3), 180, dtype=np.uint8)
        self.assertEqual(frame_difference(reference, reference)["changed_fraction"], 0)
        for color in (0, 30, 255):
            changed = reference.copy()
            changed[5:10, 5:10] = color
            self.assertGreater(
                frame_difference(reference, changed)["changed_fraction"], 0.01
            )
        with self.assertRaises(ValueError):
            frame_difference(reference, reference[:5])

    def test_randomized_repeated_effect_and_sensitivity(self) -> None:
        """Require two batches and retain effect sizes and corrected uncertainty."""
        schedule = randomized_schedule(12)
        self.assertNotEqual(schedule[0], schedule[1])
        batches: list[list[dict[str, Any]]] = [
            [
                {"order": order, "baseline": index, "action": index + 5 + index % 2}
                for index, order in enumerate(batch)
            ]
            for batch in schedule
        ]
        result = paired_statistics(batches, 8)
        self.assertTrue(result["potential_inference_channel"])
        self.assertGreater(result["batches"][0]["sensitivity_half_width"], 0)
        batches[1] = [{**pair, "action": pair["baseline"] - 5} for pair in batches[1]]
        self.assertFalse(paired_statistics(batches)["potential_inference_channel"])
        with self.assertRaises(ValueError):
            paired_statistics([batches[0]])
        with self.assertRaises(ValueError):
            paired_statistics([batches[0][:19], batches[1]])

    def test_statistics_are_derived_from_complete_timelines(self) -> None:
        """Recover paired effects from reviewed intervals and reject missing action proof."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "browser").mkdir()
            schedule = randomized_schedule(42)
            atomic_json(root / "statistics-schedule.json", {"chrome-idle": schedule})
            attempts: dict[str, Any] = {}
            events = []
            clock = 1000
            for batch, orders in enumerate(schedule, 1):
                for pair, order in enumerate(orders, 1):
                    for phase in (
                        ("baseline", "action")
                        if order == "AB"
                        else ("action", "baseline")
                    ):
                        identity = f"stat.chrome-idle.{batch}.{pair}.{phase}"
                        attempts[identity] = {
                            "id": identity,
                            "attempt": identity,
                            "session": "fixture",
                            "scenario": {
                                "id": identity,
                                "family": "statistics",
                                "proof": ["physical_action"],
                            },
                            "start_ms": clock,
                            "end_ms": clock + 15000,
                            "completion": "Observed state",
                            "review": "Reviewed timeline",
                            "proof": {"physical_action": "Confirmed"},
                        }
                        trial = attempts[identity]
                        reference = action_receipt(root, trial, ["physical_action"])
                        trial["completion"] = reference
                        trial["proof"] = {"physical_action": reference}
                        measured = interval_events(trial)
                        for sample in range(16):
                            for event_type, metric in METRICS.items():
                                measured.append(
                                    {
                                        "attempt": identity,
                                        "type": event_type,
                                        "wall_ms": clock + sample * 1000,
                                        "data": {
                                            metric: 10 if phase == "baseline" else 25
                                        },
                                    }
                                )
                        measured.sort(key=lambda event: event["wall_ms"])
                        events.extend(measured)
                        clock += 17000
            for sequence, event in enumerate(events, 1):
                event["seq"] = sequence
            atomic_json(root / "checkpoint.json", {"attempts": attempts})
            (root / "browser" / "fixture.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events), encoding="utf-8"
            )
            results = analyze_statistics(root)
            self.assertEqual(
                next(
                    result
                    for result in results
                    if result.check.endswith("animation.mean")
                ).status,
                "FAIL",
            )
            self.assertFalse(any(result.status == "PASS" for result in results))
            attempts[next(iter(attempts))]["proof"] = {}
            atomic_json(root / "checkpoint.json", {"attempts": attempts})
            self.assertTrue(
                all(
                    result.status == "INCONCLUSIVE"
                    for result in analyze_statistics(root)
                    if not result.check.endswith(".retained-failure")
                )
            )


class HTTPBoundaryTests(unittest.TestCase):
    """Keep the browser interface restricted to its own origin and evidence."""

    def test_origin_token_and_host(self) -> None:
        """Reject foreign origins, incorrect tokens, and rebound Host headers."""
        handler = ObserverHandler.__new__(ObserverHandler)
        handler.server = MagicMock(
            origin="http://127.0.0.1:4321", token="fixture-token"
        )
        for host, origin, token, expected in (
            ("127.0.0.1:4321", "http://127.0.0.1:4321", "fixture-token", True),
            ("127.0.0.1:4321", "https://foreign.example", "fixture-token", False),
            ("foreign.example", "http://127.0.0.1:4321", "fixture-token", False),
            ("127.0.0.1:4321", "http://127.0.0.1:4321", "wrong", False),
        ):
            handler.headers = Message()
            handler.headers["Host"] = host
            handler.headers["Origin"] = origin
            handler.headers["X-Audit-Token"] = token
            self.assertEqual(handler._authorized(), expected)

    def test_native_paths_are_not_served(self) -> None:
        """Reject native diagnostics and traversal even with a valid page token."""
        handler = ObserverHandler.__new__(ObserverHandler)
        handler.server = MagicMock(
            origin="http://127.0.0.1:4321", token="fixture-token"
        )
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:4321"
        handler.headers["X-Audit-Token"] = "fixture-token"
        with patch.object(handler, "send_error") as error:
            for path in (
                "/native/windows.jsonl",
                "/../metadata.json",
                "/isolated/data/config.json",
            ):
                handler.path = path
                handler.do_GET()
                error.assert_called_with(404)


class IsolationInventoryTests(unittest.TestCase):
    """Check data isolation and coverage drift decisions."""

    def test_staging_preserves_private_data(self) -> None:
        """Copy unchanged code while excluding history, credentials, cache and environment files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, run = root / "repo", root / "run"
            source = repository / "src"
            (source / "data").mkdir(parents=True)
            run.mkdir()
            (source / "que.py").write_text("# synthetic source\n")
            (source / "data" / "config.json").write_text("PRIVATE")
            (source / ".env").write_text("PRIVATE")
            metadata = stage(repository, run, "source")
            self.assertEqual((source / "data" / "config.json").read_text(), "PRIVATE")
            self.assertNotIn(
                "PRIVATE", (run / "isolated" / "data" / "config.json").read_text()
            )
            self.assertFalse((run / "isolated" / ".env").exists())
            self.assertEqual(list(metadata["hashes"]), ["que.py"])
            with patch.dict("os.environ", {"UNRELATED_SECRET": "secret"}):
                self.assertNotIn("UNRELATED_SECRET", child_environment(run))

    def test_new_control_requires_decision(self) -> None:
        """Reject a newly introduced constructor until the manifest is reviewed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            path = source / "view.py"
            path.write_text(
                "class View:\n    def build(self):\n        self.button = QPushButton('One')\n"
            )
            baseline = root / "baseline.json"
            atomic_json(baseline, discover(source))
            self.assertEqual(compare_inventory(source, baseline).status, "PASS")
            path.write_text(
                path.read_text() + "        self.other = QPushButton('Two')\n"
            )
            self.assertEqual(compare_inventory(source, baseline).status, "FAIL")

    def test_matrix_has_all_browsers_sources_and_repeats(self) -> None:
        """Keep unsupported sources and required repetitions in the coverage matrix."""
        item = Scenario(
            "capture.fixture", "show", "visible", "pixels", "exclude", family="capture"
        )
        rows = matrix([item], ["chrome", "edge", "firefox"])
        self.assertEqual({row["configuration"]["repeat"] for row in rows}, {1, 2, 3})
        self.assertEqual(
            {row["configuration"]["browser"] for row in rows},
            {"chrome", "edge", "firefox"},
        )
        self.assertEqual(
            {row["configuration"]["exclusion"] for row in rows}, {True, False}
        )
        self.assertEqual(len(HOTKEYS), 32)
        self.assertEqual(len({(mask, vk) for mask, vk, *_ in HOTKEYS}), len(HOTKEYS))
        self.assertTrue(
            any(
                row["configuration"]["capture_source"] == "Que-if-offered"
                for row in rows
            )
        )
        contracts = scenarios({"controls": {"fixture": "QPushButton()"}})
        self.assertTrue(
            all(
                item.completion and item.cleanup and item.prerequisites
                for item in contracts
            )
        )
        self.assertTrue(
            all(
                "frames" in item.proof and "physical_configuration" in item.proof
                for item in contracts
            )
        )
        pairs = [
            item.id
            for item in contracts
            if item.id.startswith("combination.")
            and not item.id.startswith("combination.triple.")
        ]
        self.assertEqual(len(pairs), 56)
        self.assertEqual(len(set(pairs)), 56)
        for left in (
            "streaming",
            "recording",
            "transcribing",
            "screen-sharing",
            "fullscreen",
            "hidden",
            "click-through",
            "provider-error",
        ):
            for right in (
                "streaming",
                "recording",
                "transcribing",
                "screen-sharing",
                "fullscreen",
                "hidden",
                "click-through",
                "provider-error",
            ):
                if left != right:
                    self.assertTrue(
                        any(
                            identity.endswith(f".order-{left}-then-{right}")
                            for identity in pairs
                        )
                    )


if __name__ == "__main__":
    unittest.main()
