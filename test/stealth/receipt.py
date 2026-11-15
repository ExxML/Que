"""Bind a reviewed physical observation to exact retained action artifacts."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stealth.evidence import artifact_path, validate_reference
from stealth.results import atomic_json


def main() -> None:
    """Create an action receipt after reviewing its before/after artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("attempt")
    parser.add_argument("--role", action="append", required=True)
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        help="Relative path:kind (for example native/video.mp4:physical-video).",
    )
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--observed-ms", required=True, type=int)
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    trial = json.loads((args.run / "checkpoint.json").read_text(encoding="utf-8"))[
        "attempts"
    ][args.attempt]
    artifacts = []
    for item in args.artifact:
        relative, kind = item.rsplit(":", 1)
        path = artifact_path(args.run, relative)
        artifacts.append(
            {
                "path": relative,
                "kind": kind,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    receipt = {
        "schema": 1,
        "attempt": args.attempt,
        "trial": trial["id"],
        "configuration": trial["configuration"],
        "roles": args.role,
        "observed_ms": args.observed_ms,
        "reviewer": args.reviewer,
        "method": "physical-review",
        "before": args.before,
        "after": args.after,
        "artifacts": artifacts,
    }
    (args.run / "receipts").mkdir(exist_ok=True)
    encoded = json.dumps(receipt, sort_keys=True).encode()
    relative = (
        f"receipts/{args.attempt}-{hashlib.sha256(encoded).hexdigest()[:16]}.json"
    )
    path = args.run / relative
    atomic_json(path, receipt)
    reference = f"receipt:{relative}#{hashlib.sha256(path.read_bytes()).hexdigest()}"
    for role in args.role:
        validate_reference(reference, role, trial, [], args.run)
    print(reference)


if __name__ == "__main__":
    main()
