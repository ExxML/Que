"""Reproduce false-pass paths against the actual evidence evaluator and JavaScript."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import test_stealth
from stealth.analysis import frame_difference
from stealth.evidence import observation_errors, validate_reference
from stealth.inventory import compare_operations, discover_operations
from stealth.results import Result, assess_trial, atomic_json
from test_stealth import action_receipt, interval_events


class AdversarialObserverTests(unittest.TestCase):
    """Extend observer fault injection with previously missed exposures and corrupt evidence."""

    def setUp(self) -> None:
        """Reuse the observer fixture without duplicating its existing test cases."""
        self.fixture = test_stealth.ObserverTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.session = self.fixture.session
        self.attempt = self.fixture.attempt

    def submit(self, fault: dict[str, object] | None = None) -> Result:
        """Complete the shared fixture with one injected browser observation."""
        return self.fixture.submit(fault)

    def test_modifier_only_leak(self) -> None:
        """Fail even when only the suppressed chord modifier reaches the website."""
        self.assertEqual(
            self.submit({"type": "keydown", "data": {"code": "ControlLeft"}}).status,
            "FAIL",
        )

    def test_one_pixel_leak(self) -> None:
        """Fail one changed pixel in a large calibrated capture region."""
        self.assertEqual(
            self.submit(
                {
                    "type": "capture-difference",
                    "data": {
                        "calibrated": True,
                        "changed_pixels": 1,
                        "changed_fraction": 0.000001,
                    },
                }
            ).status,
            "FAIL",
        )

    def test_nontext_clipboard_exposure(self) -> None:
        """Keep a copied image exposure visible even when no plaintext was returned."""
        self.assertEqual(
            self.submit(
                {
                    "type": "clipboard-read",
                    "data": {"format": "image/png", "bytes": 400, "sha256": "fixture"},
                }
            ).status,
            "FAIL",
        )

    def test_directory_metadata_exposure(self) -> None:
        """Detect granted file-name and modification metadata without a text payload."""
        self.assertEqual(
            self.submit(
                {
                    "type": "directory-read",
                    "data": {
                        "name": "cache/screenshots/screenshot1.png",
                        "bytes": 100,
                        "lastModified": 1234,
                    },
                }
            ).status,
            "FAIL",
        )

    def test_effects_have_no_blanket_exemptions(self) -> None:
        """Retain focus effects even inside ordinary-input and fullscreen workflows."""
        self.store.state["attempts"][self.attempt]["scenario"]["id"] = "input.integrity"
        self.assertEqual(self.submit({"type": "blur"}).status, "FAIL")

    def test_probe_failure_cannot_pass(self) -> None:
        """Reject a failed probe even when its proof field contains a claim."""
        self.assertEqual(
            self.submit({"type": "probe-error", "data": {"probe": "mic"}}).status,
            "INCONCLUSIVE",
        )

    def test_conflicting_duplicate(self) -> None:
        """Reject rewritten events both before and after recorder recovery."""
        event = {
            "seq": 1,
            "type": "heartbeat",
            "wall_ms": 1000,
            "attempt": self.attempt,
        }
        self.store.events(self.session, [event])
        with self.assertRaises(ValueError):
            self.store.events(self.session, [{**event, "type": "blur"}])

    def test_unknown_attempt(self) -> None:
        """Reject orphan events that could otherwise disappear from all action reports."""
        with self.assertRaises(ValueError):
            self.store.events(
                self.session,
                [{"seq": 1, "type": "blur", "wall_ms": 1000, "attempt": "unknown"}],
            )

    def test_clock_reversal(self) -> None:
        """Reject timeline reversals instead of clipping their effects out of intervals."""
        with self.assertRaises(ValueError):
            self.store.events(
                self.session,
                [
                    {"seq": 1, "type": "heartbeat", "wall_ms": 2000},
                    {"seq": 2, "type": "blur", "wall_ms": 1000},
                ],
            )

    def test_persisted_timeline_corruption_does_not_pass(self) -> None:
        """Reject changed persisted content and truncated acknowledged timelines on recovery."""
        self.assertEqual(self.submit().status, "PASS")
        path = self.fixture.path / "browser" / f"{self.session}.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8").replace("heartbeat", "altered-event"),
            encoding="utf-8",
        )
        self.assertEqual(self.store.results()[0].status, "INCONCLUSIVE")
        resumed = type(self.store)(self.fixture.path, [self.fixture.spec])
        self.assertTrue(resumed.state["errors"])


class EvidenceContractTests(unittest.TestCase):
    """Require artifact-backed completion and actual observations for negative results."""

    def test_receipt_is_bound_to_raw_artifacts_action_and_configuration(self) -> None:
        """Reject stale, changed, outside-run and cross-action artifacts."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trial = {
                "id": "fixture",
                "attempt": "fixture",
                "start_ms": 1000,
                "end_ms": 4000,
                "configuration": {"repeat": 1},
                "review": "Reviewed fixture",
            }
            reference = action_receipt(root, trial, ["physical_action"])
            trial["proof"] = {"physical_action": reference}
            trial["completion"] = reference
            events = interval_events(trial)
            self.assertEqual(
                assess_trial(trial, events, ["physical_action"], root).status, "PASS"
            )
            for altered in (
                {"attempt": "other"},
                {"configuration": {"repeat": 2}},
                {"completion": "I saw it"},
                {"start_ms": 2000},
            ):
                self.assertEqual(
                    assess_trial(
                        {**trial, **altered}, events, ["physical_action"], root
                    ).status,
                    "INCONCLUSIVE",
                )
            (root / "fixture-action.json").write_text("replaced after review")
            self.assertEqual(
                assess_trial(trial, events, ["physical_action"], root).status,
                "INCONCLUSIVE",
            )
            with self.assertRaises(ValueError):
                validate_reference(
                    "receipt:../outside.json#abc",
                    "physical_action",
                    trial,
                    events,
                    root,
                )

    def test_wrong_browser_and_missing_api_samples(self) -> None:
        """Require matching browser evidence and real probe output, independently of receipts."""
        trial = {
            "configuration": {"browser": "edge"},
            "scenario": {"proof": ["pressure_probe"]},
        }
        events = [
            {
                "type": "interval-start",
                "data": {
                    "configuration": {"browser": "edge"},
                    "userAgent": "Chrome/123",
                },
            }
        ]
        errors = observation_errors(trial, events, events, None)
        self.assertIn("selected browser matching actual user agent", errors)
        self.assertIn("actual pressure_probe samples", errors)

    def test_camera_cannot_be_substituted_by_display_capture(self) -> None:
        """Reject display PNGs used as evidence for an unmeasured camera boundary."""
        trial = {"scenario": {"proof": ["camera_probe"]}}
        events = [{"type": "saved-frame", "data": {"kind": "display"}}]
        self.assertIn(
            "retained actual camera frame, not a display capture",
            observation_errors(trial, events, events, None),
        )
        events = [{"type": "saved-frame", "data": {"kind": "camera"}}]
        self.assertIn(
            "retained and unmodified action frame artifacts",
            observation_errors(trial, events, events, None),
        )

    def test_capture_claim_without_calibration_images(self) -> None:
        """Reject hand-entered capture proof with absent source controls and retained frames."""
        trial = {"start_ms": 1000, "scenario": {"proof": ["frames"]}}
        events = [
            {"type": "interval-start", "wall_ms": 1000, "data": {}},
            {"type": "capture-frame", "wall_ms": 1001, "data": {"calibrated": True}},
        ]
        self.assertIn(
            "retained source-matched baseline, positive and restored negative calibration",
            observation_errors(trial, events, events, None),
        )

    def test_one_level_color_difference_is_observable(self) -> None:
        """Preserve a single low-contrast pixel instead of hiding it below a threshold."""
        baseline = np.zeros((100, 100, 3), dtype=np.uint8)
        changed = baseline.copy()
        changed[50, 50, 1] = 1
        result = frame_difference(baseline, changed)
        self.assertEqual(result["changed_pixels"], 1)
        self.assertEqual(result["max_channel_delta"], 1)

    def test_new_failure_handler_requires_inventory_decision(self) -> None:
        """Detect a background/failure path addition even when no UI control changes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src"
            source.mkdir()
            path = source / "backend.py"
            path.write_text("def run():\n    return 1\n")
            baseline = root / "operations.json"
            atomic_json(baseline, discover_operations(source))
            self.assertEqual(compare_operations(source, baseline).status, "PASS")
            path.write_text(
                "def run():\n    try:\n        return 1\n    except OSError:\n        return 0\n"
            )
            self.assertEqual(compare_operations(source, baseline).status, "FAIL")

    def test_javascript_single_pixel_and_unthrottled_frames(self) -> None:
        """Run the actual JavaScript detector against tiny leaks and pending frame uploads."""
        script = Path(__file__).parent / "stealth" / "detector_test.js"
        process = subprocess.run(
            ["node", str(script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


if __name__ == "__main__":
    unittest.main()
