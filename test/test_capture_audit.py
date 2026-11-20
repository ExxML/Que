"""Reject misleading capture evidence and verify isolated automation lifecycle behavior."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
from capture.adapters import (
    LocalShareAdapter,
    OBSAdapter,
    WindowsRecorderAdapter,
    create_adapter,
    secret,
)
from capture.audio import audio_result
from capture.automation import dedicated_profile
from capture.contracts import APPLICATIONS, SURFACES, Capture, Config, matrix
from capture.detector import (
    Detector,
    identity,
    normalize,
    synthetic_frame,
    synthetic_sequence,
)
from capture.diagnostics import focus_result, inventory_result
from capture.media import executable, frames
from capture.runner import assess, preflight, report, run_cell
from stealth.results import Result, atomic_json, verdict


class CalibrationTests(unittest.TestCase):
    """Exercise calibrated negative, positive, stale, and transient video observations."""

    calibrated: Detector

    @classmethod
    def setUpClass(cls) -> None:
        """Build one complete calibrated sequence shared by independent detector copies."""
        cls.calibrated = Detector(12345)
        for frame, timestamp in synthetic_sequence(12345, (0, 1, 2, 3, 4, 5, 6, 10)):
            cls.calibrated.feed(frame, timestamp)

    def setUp(self) -> None:
        """Keep each adversarial observation independent."""
        self.detector = copy.deepcopy(self.calibrated)

    def test_complete_negative_requires_all_controls(self) -> None:
        """Accept clean output only when every positive and held-out control worked."""
        self.assertEqual(self.detector.health([10], 3), [])
        self.assertEqual(self.detector.observations[10].changed, 0)
        for phase in (2, 3, 4, 5):
            self.assertGreater(self.detector.observations[phase].changed, 0)
        del self.detector.observations[3]
        self.assertTrue(self.detector.health([10], 3))

    def test_single_frame_translucency_black_border_and_text(self) -> None:
        """Detect brief exposures without requiring them to persist across frames."""
        for kind in ("translucent", "black", "border", "text"):
            with self.subTest(kind=kind):
                detector = copy.deepcopy(self.calibrated)
                frame = synthetic_frame(12345, 10, 300)
                if kind == "translucent":
                    frame[200:300, 200:400] = (frame[200:300, 200:400] * 0.85).astype(
                        np.uint8
                    )
                elif kind == "black":
                    frame[200:300, 200:400] = 0
                elif kind == "border":
                    frame[200:201, 200:400] = 255
                else:
                    frame[220:224, 200:240] = 255
                detector.feed(frame, 25.6)
                self.assertEqual(detector.observations[10].changed, 1)
                self.assertIn(10, detector.examples)

    def test_wrong_source_blank_and_missing_markers(self) -> None:
        """Reject quiet output from an unrelated source or an obscured fixture."""
        for frame in (
            synthetic_frame(9876, 10, 300),
            np.zeros((720, 960, 3), dtype=np.uint8),
        ):
            detector = copy.deepcopy(self.calibrated)
            detector.feed(frame, 25.6)
            self.assertTrue(detector.health([10], 3))

    def test_frozen_counter_and_frame_gap(self) -> None:
        """Reject a recording that repeats old pixels or drops an observation interval."""
        frame = synthetic_frame(12345, 10, 300)
        self.detector.feed(frame, 25.6)
        self.detector.feed(frame, 26.8)
        self.assertIn("Frozen source counter.", self.detector.health([10], 3))
        self.assertGreater(self.detector.observations[10].largest_gap, 0.5)

    def test_nonmonotonic_and_changed_baseline(self) -> None:
        """Reject a reset timeline or an attempt to recalibrate after Que is visible."""
        self.detector.feed(synthetic_frame(12345, 0, 300), 25.6)
        self.detector.feed(synthetic_frame(12345, 10, 301), 24.0)
        self.assertGreaterEqual(len(self.detector.errors), 2)

    def test_setup_frames_cannot_substitute_for_a_matched_baseline(self) -> None:
        """Count setup frames but require the real nonce and initial calibration phase."""
        detector = Detector(12345)
        detector.feed(np.zeros((720, 960, 3), dtype=np.uint8), 0.0)
        detector.feed(synthetic_frame(12345, 7, 1), 0.1)
        self.assertEqual(detector.setup_frames, 2)
        self.assertIn(
            "never showed the calibrated fixture", detector.health([10], 3)[0]
        )
        detector.feed(synthetic_frame(12345, 0, 2), 0.2)
        self.assertTrue(detector.started)
        detector.feed(synthetic_frame(999, 0, 3), 0.3)
        self.assertIn("Wrong capture source nonce.", detector.errors)

    def test_scaling_and_letterboxing_preserve_source_identity(self) -> None:
        """Locate a resized source inside a larger receiver canvas."""
        frame = synthetic_frame(0xA123FFFF, 19, 0x123456)
        canvas = np.zeros((800, 1200, 3), dtype=np.uint8)
        canvas[30:750, 100:1060] = frame
        self.assertEqual(identity(normalize(canvas)), (0xA123FFFF, 19, 0x123456))


class LifecycleTests(unittest.TestCase):
    """Keep setup gaps and cleanup failures separate from successful capture observations."""

    def test_full_matrix_contains_every_app_and_valid_obs_methods(self) -> None:
        """Expand real capture paths and preserve repetitions and displays."""
        cells = matrix(list(APPLICATIONS), (0, 1), 3)
        self.assertEqual({cell.app for cell in cells}, set(APPLICATIONS))
        self.assertEqual(len({cell.id for cell in cells}), len(cells))
        self.assertFalse(
            any(
                cell.app == "obs" and cell.source == "monitor" and cell.mode == "bitblt"
                for cell in cells
            )
        )
        self.assertTrue(
            any(cell.app == "zoom-desktop" and cell.mode == "legacy" for cell in cells)
        )

    def test_missing_accounts_cannot_pass_preflight(self) -> None:
        """Report unconfigured meeting clients as incomplete without opening a browser."""
        with (
            patch("capture.runner.executable", create=True),
            patch("subprocess.run") as command,
        ):
            command.return_value.stdout = "test decoder"
            results = preflight(Config({}), ["meet-chrome", "zoom-desktop"])
        self.assertEqual(verdict(results)[1], 2)

    def test_profile_requires_explicit_marker(self) -> None:
        """Refuse accidental reuse of an ordinary browser profile."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with self.assertRaises(ValueError):
                dedicated_profile(str(path))
            (path / ".que-capture-profile").touch()
            self.assertEqual(dedicated_profile(str(path)), path.resolve())

    def test_obs_busy_session_is_left_untouched(self) -> None:
        """Refuse to commandeer an existing OBS recording or broadcast."""
        adapter = OBSAdapter({"port": 4455, "sources": {}})
        fake = MagicMock()
        fake.get_record_status.return_value.output_active = True
        module = MagicMock()
        module.ReqClient.return_value = fake
        with (
            patch("capture.adapters.importlib.import_module", return_value=module),
            self.assertRaisesRegex(RuntimeError, "already recording"),
        ):
            adapter.start(
                matrix(["obs"], (0,), 1)[0], Path("unused"), "http://localhost/"
            )
        adapter.close()
        fake.stop_record.assert_not_called()
        fake.create_scene.assert_not_called()
        fake.disconnect.assert_called_once()

    def test_obs_restores_scene_after_failed_source_creation(self) -> None:
        """Run every OBS restore step even when acquisition and one restore both fail."""
        adapter = OBSAdapter(
            {
                "port": 4455,
                "sources": {
                    "monitor.auto.0": {"kind": "monitor_capture", "settings": {}}
                },
            }
        )
        fake = MagicMock()
        fake.get_record_status.return_value.output_active = False
        fake.get_stream_status.return_value.output_active = False
        fake.get_current_program_scene.return_value.scene_name = "Original"
        fake.get_record_directory.return_value.record_directory = "Original recordings"
        fake.create_input.side_effect = RuntimeError("No capture source")
        # OBS answers OutputRunning (500) while a recording is still stopping.
        fake.set_record_directory.side_effect = RuntimeError("OutputRunning")
        module = MagicMock()
        module.ReqClient.return_value = fake
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("capture.adapters.importlib.import_module", return_value=module),
        ):
            with self.assertRaises(RuntimeError):
                adapter.start(
                    matrix(["obs"], (0,), 1)[0], Path(temporary), "http://localhost/"
                )
            saved = json.loads(
                (Path(temporary) / "private" / "obs-restore.json").read_text(
                    encoding="utf-8"
                )
            )
            with self.assertRaisesRegex(RuntimeError, "restore needs attention"):
                adapter.close()
        self.assertEqual(saved["record_directory"], "Original recordings")
        # OBS rejects an input named like an existing scene.
        self.assertNotEqual(fake.create_input.call_args.args[1], adapter.scene)
        fake.set_current_program_scene.assert_called_once_with("Original")
        fake.remove_scene.assert_called_once_with(adapter.scene)
        fake.disconnect.assert_called_once()

    def test_obs_refuses_leftovers_from_an_earlier_audit(self) -> None:
        """Never save an earlier audit's scene and folder as the user's settings."""
        adapter = OBSAdapter({"port": 4455, "sources": {}})
        fake = MagicMock()
        fake.get_record_status.return_value.output_active = False
        fake.get_stream_status.return_value.output_active = False
        fake.get_scene_list.return_value.scenes = [
            {"sceneName": "Scene"},
            {"sceneName": "Que-capture-audit-0123"},
        ]
        module = MagicMock()
        module.ReqClient.return_value = fake
        with (
            patch("capture.adapters.importlib.import_module", return_value=module),
            self.assertRaisesRegex(RuntimeError, "earlier audit"),
        ):
            adapter.start(
                matrix(["obs"], (0,), 1)[0], Path("unused"), "http://localhost/"
            )
        adapter.close()
        fake.create_scene.assert_not_called()
        fake.set_record_directory.assert_not_called()

    def test_windows_recorder_refuses_ambiguous_output(self) -> None:
        """Never select a recording belonging to another concurrent capture."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            adapter = WindowsRecorderAdapter(
                "game-bar", {"recordings": str(directory), "start": [], "stop": []}
            )
            with patch("capture.adapters.native_steps"):
                adapter.start(matrix(["game-bar"], (0,), 1)[0], directory, "")
                (directory / "one.mp4").touch()
                (directory / "two.mp4").touch()
                with self.assertRaisesRegex(RuntimeError, "Multiple"):
                    adapter.finish()

    def test_missing_or_changed_artifacts_stay_incomplete(self) -> None:
        """Reject a claimed completion without matching raw action and video evidence."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "attempt"
            directory.mkdir()
            cell = matrix(["meet-chrome"], (0,), 1)[0]
            from dataclasses import asdict

            atomic_json(
                directory / "attempt.json",
                {
                    "cell": asdict(cell),
                    "nonce": 123,
                    "duration": 3,
                    "state": "complete",
                    "artifacts": {"receiver.webm": "invalid"},
                    "media": "receiver.webm",
                },
            )
            results = assess(root, directory, Config({}))
            self.assertEqual(verdict(results)[1], 2)
            self.assertFalse(any(result.status == "PASS" for result in results))

    def test_local_share_needs_no_accounts_or_picker(self) -> None:
        """Cover Chromium sharing without meeting setup or inapplicable picker gaps."""
        cells = matrix(["chrome-local", "edge-local"], (0,), 1)
        self.assertEqual(
            {(cell.app, cell.source) for cell in cells},
            {
                (app, source)
                for app in ("chrome-local", "edge-local")
                for source in ("monitor", "window", "tab")
            },
        )
        adapter = create_adapter("edge-local", {})
        self.assertIsInstance(adapter, LocalShareAdapter)
        self.assertEqual(adapter.probe(), [])
        from dataclasses import asdict

        # Only tab shares carry audio when Chromium auto-selects the source.
        for cell, audio in ((cells[0], False), (cells[1], False), (cells[2], True)):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / "attempt"
                directory.mkdir()
                atomic_json(
                    directory / "attempt.json",
                    {"cell": asdict(cell), "nonce": 1, "duration": 3},
                )
                names = {
                    result.check.rsplit(".", 1)[1]
                    for result in assess(root, directory, Config({}, monitors=(0,)))
                }
            self.assertEqual("audio" in names, audio, cell.source)
            self.assertFalse(names & {"picker", "share-restart", "source-change"})

    def test_audio_accepts_healthy_sources_that_report_no_status(self) -> None:
        """Treat opened streams without a health message as healthy, and unopened ones as gaps."""
        detector = Detector(1)
        stopped: dict[str, Any] = {"recording": False}
        for sources, reason in (
            (["you", "them"], "Missing audio measurement interval"),
            (["you"], "Real microphone and loopback"),
        ):
            states = [
                stopped,
                {"recording": True, "source_health": {}, "audio_sources": sources},
                stopped,
            ]
            result = audio_result(Path("unused.webm"), detector, states, "ffmpeg")
            self.assertEqual(result.status, "INCONCLUSIVE")
            self.assertTrue(result.reason.startswith(reason))

    def test_secret_prefers_environment_and_is_never_exported(self) -> None:
        """Read OBS credentials without placing them where child processes inherit them."""
        name = "QUE_CAPTURE_TEST_UNSET_SECRET"
        self.assertEqual(secret(name), "")
        with patch.dict("os.environ", {name: "from-environment"}):
            self.assertEqual(secret(name), "from-environment")
        self.assertNotIn(name, os.environ)

    def test_picker_covers_anonymous_entries_and_positive_control(self) -> None:
        """Refuse empty or insensitive inventories and notice unnamed window additions."""
        ordinary = [
            {"controls": [{"text": "Que capture audit fixture", "type": "ListItem"}]}
        ]
        self.assertEqual(
            inventory_result(
                "picker", ordinary, ordinary, "Que capture audit fixture"
            ).status,
            "PASS",
        )
        anonymous = copy.deepcopy(ordinary)
        anonymous[0]["controls"].append({"text": "", "type": "ListItem"})
        self.assertEqual(
            inventory_result(
                "picker", ordinary, anonymous, "Que capture audit fixture"
            ).status,
            "INCONCLUSIVE",
        )
        self.assertEqual(
            inventory_result("picker", [], [], "Que capture audit fixture").status,
            "INCONCLUSIVE",
        )

    def test_focus_loss_is_a_failure(self) -> None:
        """Retain an observed foreground transfer rather than dismissing it as setup."""
        self.assertEqual(
            focus_result(5, [{"ok": True, "foreground": 7}]).status, "FAIL"
        )
        self.assertEqual(focus_result(0, []).status, "INCONCLUSIVE")

    def test_config_rejects_unknown_apps_and_bad_repetitions(self) -> None:
        """Prevent malformed configuration from silently narrowing required coverage."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for value in (
                {"schema": 1, "apps": {"unknown": {}}},
                {"schema": 1, "apps": {}, "repeats": 0},
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    Config.read(path)


class RunnerTests(unittest.TestCase):
    """Verify measurement boundaries and immutable findings without joining any service."""

    def test_exposure_controls_wait_for_their_painted_phase(self) -> None:
        """Keep intentional exposure out of protected measurement intervals."""
        phase = {"current": 7, "painted": -1, "exposures": 0}
        fixture = MagicMock()
        fixture.log = []
        fixture.process.pid = 123
        fixture.process.returncode = 0
        backdrop = MagicMock()
        adapter = MagicMock()
        adapter.probe.return_value = []
        adapter.operate.return_value = False
        paths: list[Path] = []

        def advance(value: int) -> None:
            """Advance the requested source phase without acknowledging its paint."""
            phase["current"] = value

        def presented(_source: str) -> None:
            """Acknowledge the phase only when the runner waits for its source."""
            phase["painted"] = phase["current"]

        def command(name: str, **values: Any) -> dict[str, Any]:
            """Fail if capture exclusion is disabled before the control marker is painted."""
            if name == "capture" and values.get("hidden") is False:
                self.assertGreaterEqual(phase["current"], 100)
                self.assertEqual(phase["current"], phase["painted"])
                phase["exposures"] += 1
            response = {
                "ok": True,
                "foreground": 7,
                "visible": 1,
                "surface": values.get("surface"),
            }
            fixture.log.append(
                {"request": {"command": name, **values}, "response": response}
            )
            return response

        def start(_cell: object, directory: Path, _url: str) -> None:
            """Create an explicit mock acquisition artifact without asserting capture success."""
            path = directory / "receiver.webm"
            path.write_bytes(b"fixture transport only")
            paths.append(path)

        backdrop.phase.side_effect = advance
        backdrop.wait_presented.side_effect = presented
        fixture.command.side_effect = command
        adapter.start.side_effect = start
        adapter.finish.side_effect = lambda: Capture(paths[0], "test-only-fixture")
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("capture.runner.create_adapter", return_value=adapter),
            patch("capture.runner.Backdrop", return_value=backdrop),
            patch("capture.runner.BrowserStage"),
            patch("capture.runner.FixtureProcess", return_value=fixture),
            patch("capture.runner.media_metadata", return_value={}),
        ):
            root = Path(temporary)
            directory = run_cell(
                root,
                matrix(["meet-chrome"], (0,), 1)[0],
                Config({}, monitors=(0,)),
                {"monitors": [{}]},
            )
            acquired = json.loads(
                (directory / "attempt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(acquired["state"], "complete")
            self.assertEqual(acquired["shutdown_exit"], 0)
            self.assertEqual(phase["exposures"], len(SURFACES) - 1)
            self.assertIn("actions.json", acquired["artifacts"])

    def test_missing_retry_cannot_erase_an_earlier_failure(self) -> None:
        """Retain an established finding even when later evidence disappears."""
        from dataclasses import asdict

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config({}, monitors=(0,))
            cell = matrix(["meet-chrome"], (0,), 1)[0]
            atomic_json(root / "metadata.json", {"configuration": config.fingerprint()})
            atomic_json(root / "matrix.json", [asdict(cell)])
            atomic_json(
                root / "findings.json",
                [
                    asdict(
                        Result(
                            f"{cell.id}.main",
                            "FAIL",
                            "Previously observed exposed frame.",
                        )
                    )
                ],
            )
            self.assertEqual(report(root, config), 1)
            result = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["status"] == "FAIL" for item in result["results"]))


class MediaTests(unittest.TestCase):
    """Exercise real compressed-video decoding when the optional decoder is installed."""

    def test_encoded_frames_preserve_pts_and_identity(self) -> None:
        """Decode an actual H.264 file with every frame and its presentation timestamp."""
        try:
            ffmpeg = executable()
        except (ImportError, FileNotFoundError):
            self.skipTest("Optional capture decoder is not installed.")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.mp4"
            payload = b"".join(
                synthetic_frame(12, 10, tick).tobytes() for tick in range(4)
            )
            subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-s",
                    "960x720",
                    "-r",
                    "10",
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    str(path),
                ],
                input=payload,
                capture_output=True,
                timeout=30,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            decoded = list(frames(path, ffmpeg))
            self.assertEqual(len(decoded), 4)
            self.assertEqual(
                [round(timestamp, 2) for _, timestamp in decoded], [0, 0.1, 0.2, 0.3]
            )
            self.assertEqual(
                [identity(normalize(frame))[2] for frame, _ in decoded], [0, 1, 2, 3]
            )


if __name__ == "__main__":
    unittest.main()
