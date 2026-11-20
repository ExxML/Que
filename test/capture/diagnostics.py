"""Evaluate shell/picker inventories and foreground ownership independently of video."""

from __future__ import annotations

from collections import Counter
from typing import Any

from stealth.results import Result


def inventory_result(
    name: str,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    positive_title: str,
) -> Result:
    """Require a visible ordinary source and compare every named and anonymous UIA entry."""

    def entries(rows: list[dict[str, Any]]) -> Counter[tuple[str, str, str]]:
        """Count entries including duplicates and anonymous controls."""
        return Counter(
            (
                str(control.get("text", "")),
                str(control.get("type", "")),
                str(control.get("automation_id", "")),
            )
            for row in rows
            for control in row.get("controls", [])
        )

    baseline, observed = entries(before), entries(after)
    exposed = [
        text for text, _, _ in observed if text.strip().casefold() in ("que", "que.exe")
    ]
    if exposed:
        return Result(
            name,
            "FAIL",
            "Que appears as an identifiable shell or share-source entry.",
            kind="capture",
        )
    if (
        not positive_title
        or not any(positive_title in text for text, _, _ in baseline)
        or not any(positive_title in text for text, _, _ in observed)
    ):
        return Result(
            name,
            "INCONCLUSIVE",
            "The ordinary fixture window is missing from the inventory; enumeration sensitivity is unverified.",
            kind="capture",
        )
    if observed != baseline:
        return Result(
            name,
            "INCONCLUSIVE",
            "The shell/picker inventory changed, including possible anonymous entries; inspect retained inventories.",
            kind="capture",
        )
    return Result(
        name,
        "PASS",
        "Complete accessible inventories match with Que absent/present, including anonymous entries; the ordinary fixture remains listed.",
        kind="capture",
    )


def focus_result(baseline: int, actions: list[dict[str, Any]]) -> Result:
    """Reject an unobserved foreground and report any action-caused foreground transfer."""
    if (
        not baseline
        or not actions
        or any(
            not action.get("ok") or not action.get("foreground") for action in actions
        )
    ):
        return Result(
            "focus",
            "INCONCLUSIVE",
            "Missing foreground or completed-action observation.",
            kind="capture",
        )
    if any(action["foreground"] != baseline for action in actions):
        return Result(
            "focus",
            "FAIL",
            "Foreground ownership changed while operating Que's fixture surfaces.",
            kind="capture",
        )
    return Result(
        "focus",
        "PASS",
        "Foreground ownership stayed unchanged at the recorded action boundaries.",
        kind="capture",
    )
