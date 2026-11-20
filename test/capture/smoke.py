"""Validate every real Windows fixture action without claiming meeting or recording coverage."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stealth.isolation import stage
from stealth.results import atomic_json

from capture.contracts import SURFACES
from capture.process import FixtureProcess


def main() -> int:
    """Stage synthetic data, exercise all surfaces, and retain action acknowledgements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    stage(Path(__file__).resolve().parents[2], directory, "source")
    fixture = FixtureProcess(directory)
    try:
        for surface in SURFACES:
            print(surface, flush=True)
            fixture.command("surface", surface=surface)
            if surface != "shutdown":
                # The runner reopens each surface for its monitor exposure control.
                fixture.command("hide")
                fixture.command("surface", surface=surface)
            fixture.command("capture", hidden=False)
            fixture.command("capture", hidden=True)
            fixture.command("hide")
        atomic_json(
            directory / "fixture-report.json",
            {
                "scope": "Real Qt action smoke only; no received capture validation.",
                "actions": fixture.log,
                "displays": fixture.ready["displays"],
            },
        )
        print(json.dumps({"completed": len(SURFACES)}))
        return 0
    finally:
        fixture.close()
        (directory / "fixture-errors.txt").write_text(
            "\n".join(fixture.errors), encoding="utf-8"
        )


if __name__ == "__main__":
    sys.exit(main())
