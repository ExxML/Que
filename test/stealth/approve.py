"""Update the reviewed stealth baselines without refreshing them wholesale.

    python test/stealth/approve.py operations                List unreviewed operation changes.
    python test/stealth/approve.py operations IDENTITY ...   Approve exactly the named, reviewed changes.
    python test/stealth/approve.py operations --moved        Approve code that only moved to another line.
    python test/stealth/approve.py prune-gaps RUN            Drop known execution gaps that RUN executed.

The offline compliance audit fails with "Unreviewed operation changes: <identity>, ..." when source
code changes; review each change for stealth before approving it. Unnamed changes stay unapproved.
It also fails when code that no offline test executes appears, and when tests start executing a
known gap; prune-gaps only ever removes gaps, so new untested code needs a test, not an approval.
"""

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPOSITORY / "test")]

from stealth.coverage import KNOWN_GAPS, gap_key
from stealth.inventory import discover_operations
from stealth.results import atomic_json, read_json

APPROVED = REPOSITORY / "test" / "stealth" / "operations.json"


def main() -> int:
    """Run the subcommand named on the command line.

    Returns:
        int: 0 on success, 1 on a usage error or an identity with no change to approve.
    """
    if sys.argv[1:2] == ["operations"]:
        names = [name for name in sys.argv[2:] if name != "--moved"]
        return _approve_operations(names, "--moved" in sys.argv[2:])
    if sys.argv[1:2] == ["prune-gaps"] and len(sys.argv) == 3:
        return _prune_gaps(Path(sys.argv[2]))
    print(__doc__, file=sys.stderr)
    return 1


def _approve_operations(names: list[str], moved: bool = False) -> int:
    """List unreviewed operation changes, or approve the named identities.

    Args:
        names (list[str]): The reviewed identities to approve; empty to only list changes.
        moved (bool): Whether to also approve operations that only moved to another line.

    Returns:
        int: 0 on success, 1 if a named identity has no change to approve.
    """
    current = discover_operations(REPOSITORY / "src")
    baseline: dict[str, Any] = json.loads(APPROVED.read_text(encoding="utf-8"))
    changes = sorted(
        identity
        for identity in current.keys() | baseline.keys()
        if current.get(identity) != baseline.get(identity)
    )
    if unknown := [name for name in names if name not in changes]:
        print(f"No unreviewed change for: {', '.join(unknown)}", file=sys.stderr)
        return 1

    # An identity whose old and new contents are both code that only changed line has nothing to review
    shifted = {_signature(baseline[k]) for k in changes if k in baseline} & {
        _signature(current[k]) for k in changes if k in current
    }
    moves = [
        identity
        for identity in changes
        if (identity not in baseline or _signature(baseline[identity]) in shifted)
        and (identity not in current or _signature(current[identity]) in shifted)
    ]
    reviewable = [identity for identity in changes if identity not in moves]
    if moved:
        names = sorted({*names, *moves})

    for identity in names or reviewable:
        before, after = baseline.get(identity), current.get(identity)
        if before and after and before["expression"] == after["expression"]:
            detail = f"body changed: review src/{after['source']} line {after['line']} (git diff, git log -p)"
        else:
            detail = f"{before['expression'] if before else '(new)'}  ->  {after['expression'] if after else '(removed)'}"
        print(f"{identity}\n  {detail}")
    if not names:
        print(
            f"\n{len(reviewable)} unreviewed change(s). Review each for stealth, then pass its identity to approve it."
        )
        if moves:
            print(
                f"{len(moves)} more only moved to another line; approve them with --moved."
            )
        return 0

    # Keep the discovery order; take named entries from the source and everything else from the approved file.
    approved = {
        identity: current[identity] if identity in names else baseline[identity]
        for identity in current
        if identity in names or identity in baseline
    }
    approved.update(
        {
            identity: baseline[identity]
            for identity in baseline
            if identity not in current and identity not in names
        }
    )
    atomic_json(APPROVED, approved)
    print(f"\nApproved {len(names)} change(s).")
    return 0


def _signature(entry: dict[str, Any]) -> tuple[str, ...]:
    """Identify an operation regardless of the line it is on.

    Args:
        entry (dict[str, Any]): An operation from operations.json.

    Returns:
        tuple[str, ...]: Its category, file, opening line and semantic hash.
    """
    return (entry["category"], entry["source"], entry["expression"], entry["sha256"])


def _prune_gaps(run: Path) -> int:
    """Remove known gaps that a compliance run executed; never add new ones.

    Args:
        run (Path): A compliance output directory containing native/execution-gaps.json.

    Returns:
        int: 0 on success.
    """
    measured = Counter(map(gap_key, read_json(run / "native" / "execution-gaps.json")))
    known = Counter(read_json(KNOWN_GAPS))
    kept = known & measured
    atomic_json(KNOWN_GAPS, sorted(kept.elements()))
    print(
        f"Removed {sum((known - kept).values())} covered gap(s); {sum(kept.values())} remain."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
