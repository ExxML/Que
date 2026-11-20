"""Preflight, run, and replay evidence from real meeting and recording applications."""

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from capture.contracts import APPLICATIONS, Cell, Config, matrix
from capture.runner import preflight, report, run_cell, source_hashes
from stealth.isolation import digest, stage
from stealth.results import atomic_json, write_reports

REPOSITORY = Path(__file__).resolve().parents[1]


def main() -> int:
    """Require explicit live execution and preserve missing coverage in every report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "run", "report"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--apps",
        help="Comma-separated application IDs; defaults to the full required matrix.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    try:
        config = Config.read(args.config)
        apps = args.apps.split(",") if args.apps else list(APPLICATIONS)
        if (
            not apps
            or len(apps) != len(set(apps))
            or any(app not in APPLICATIONS for app in apps)
        ):
            raise ValueError(
                "Select distinct application IDs from the documented matrix."
            )
        root = (
            args.resume
            or args.output
            or REPOSITORY
            / ".stealth-runs"
            / f"capture-{time.strftime('%Y%m%d-%H%M%S')}"
        ).resolve()
        if args.command == "report":
            return report(root, config)
        if args.command == "preflight":
            root.mkdir(parents=True, exist_ok=False)
            return write_reports(
                root,
                {
                    "mode": "capture-preflight",
                    "scope": "Setup checks only; no capture validation.",
                },
                preflight(config, apps),
            )
        if sys.platform != "win32":
            raise RuntimeError("Live capture requires an unlocked Windows desktop.")
        if config.target != "source":
            raise ValueError(
                "Automated widget fixtures require target=source; packaged evidence is separate."
            )
        from stealth.native import environment

        desktop = environment()
        monitors = config.monitors or tuple(range(len(desktop["monitors"])))
        if not monitors or any(index >= len(desktop["monitors"]) for index in monitors):
            raise ValueError("A configured display is unavailable.")
        config.monitors = monitors
        if args.resume:
            metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
            if metadata["configuration"] != config.fingerprint() or metadata[
                "harness"
            ] != source_hashes(REPOSITORY):
                raise ValueError("Configuration or harness changed; start a new run.")
            if (
                metadata["apps"] != apps
                or metadata["environment"]["monitors"] != desktop["monitors"]
            ):
                raise ValueError(
                    "Application selection or display layout changed; start a new run."
                )
            for path, expected in metadata["source"].items():
                if (
                    digest(root / "isolated" / path) != expected
                    or digest(REPOSITORY / "src" / path) != expected
                ):
                    raise ValueError(
                        "Staged or production source changed; start a new run."
                    )
        else:
            root.mkdir(parents=True, exist_ok=False)
            staged = stage(REPOSITORY, root, "source")
            atomic_json(
                root / "metadata.json",
                {
                    "mode": "capture",
                    "apps": apps,
                    "configuration": config.fingerprint(),
                    "harness": source_hashes(REPOSITORY),
                    "source": staged["hashes"],
                    "environment": desktop,
                    "scope": "Source widget fixtures on this desktop. Physical hotkeys, cloud recording, and packaged-build certification are separate.",
                },
            )
            atomic_json(
                root / "matrix.json",
                [asdict(cell) for cell in matrix(apps, monitors, config.repeats)],
            )
        cells = matrix(apps, monitors, config.repeats)
        # Resume reruns only cells whose acquisition never completed.
        acquired = {
            Cell(**attempt["cell"]).id
            for path in (root / "attempts").glob("*/attempt.json")
            if (attempt := json.loads(path.read_text(encoding="utf-8")))["state"]
            == "complete"
        }
        for number, cell in enumerate(cells, 1):
            print(f"[{number}/{len(cells)}] {cell.id}", flush=True)
            if cell.id in acquired:
                print("  acquired earlier; skipped", flush=True)
                continue
            run_cell(root, cell, config, desktop)
        code = report(root, config)
        print(f"Capture report: {root / 'report.html'}", flush=True)
        return code
    except KeyboardInterrupt:
        print(
            "Interrupted; completed evidence is retained for report or resume.",
            file=sys.stderr,
        )
        return 2
    except Exception as error:  # noqa: BLE001  # Keep the documented setup-error exit code at the CLI boundary.
        print(
            f"Capture audit setup/report error: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
