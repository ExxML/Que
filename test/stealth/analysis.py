"""Analyze calibrated frames and independently randomized paired measurements."""

import random
from typing import Any

import numpy as np
from numpy.typing import NDArray


def frame_difference(
    reference: NDArray[np.uint8], frame: NDArray[np.uint8]
) -> dict[str, Any]:
    """Measure changed pixels including black occlusion and missing underlying content."""
    if reference.shape != frame.shape or reference.size == 0:
        raise ValueError("Frames must have identical nonempty dimensions.")
    delta = np.abs(frame.astype(np.int16) - reference.astype(np.int16))
    changed = np.max(delta, axis=-1) > 0
    return {
        "changed_pixels": int(np.count_nonzero(changed)),
        "max_channel_delta": int(np.max(delta)),
        "changed_fraction": float(np.mean(changed)),
        "mean_absolute_error": float(np.mean(delta)),
        "black_fraction": float(np.mean(np.max(frame[..., :3], axis=-1) < 8)),
    }


def paired_statistics(
    batches: list[list[dict[str, Any]]], comparisons: int = 1
) -> dict[str, Any]:
    """Estimate paired effects with Bonferroni-corrected bootstrap intervals per batch.

    Args:
        batches (list[list[dict[str, Any]]]): Independently randomized baseline/action pairs.
        comparisons (int): Total hypotheses in the experiment, including other metrics.

    Returns:
        dict[str, Any]: Effects, uncertainty, and detectable interval widths.
    """
    if (
        len(batches) != 2
        or any(len(batch) < 20 for batch in batches)
        or comparisons < 1
    ):
        raise ValueError(
            "Require two independent batches of at least 20 pairs and a comparison count."
        )
    summaries: list[dict[str, Any]] = []
    for index, batch in enumerate(batches):
        if {item["order"] for item in batch} != {"AB", "BA"}:
            raise ValueError("Both randomized orders must occur in each batch.")
        values = np.array(
            [float(item["action"]) - float(item["baseline"]) for item in batch]
        )
        if not np.all(np.isfinite(values)):
            raise ValueError("Nonfinite measurements are incomplete evidence.")
        rng = np.random.default_rng(9281 + index)
        draws = np.mean(
            rng.choice(values, (max(10000, comparisons * 2000), len(values))), axis=1
        )
        low, high = np.quantile(draws, [0.025 / comparisons, 1 - 0.025 / comparisons])
        sd = float(np.std(values, ddof=1))
        summaries.append(
            {
                "pairs": len(batch),
                "mean_difference": float(np.mean(values)),
                "standardized_effect": float(np.mean(values)) / sd if sd else None,
                "interval": [float(low), float(high)],
                "sensitivity_half_width": float(high - low) / 2,
            }
        )
    intervals = [item["interval"] for item in summaries]
    reproduced = all(interval[0] > 0 for interval in intervals) or all(
        interval[1] < 0 for interval in intervals
    )
    return {
        "batches": summaries,
        "comparisons": comparisons,
        "family_alpha": 0.05,
        "potential_inference_channel": reproduced,
        "interpretation": "A reproducible difference does not identify Que; compare unrelated load controls.",
    }


def randomized_schedule(seed: int, pairs: int = 20) -> list[list[str]]:
    """Generate independent counterbalanced orders before collecting any measurements."""
    if pairs < 20:
        raise ValueError("At least 20 pairs per batch are required.")
    rng = random.Random(seed)
    batches = []
    for _ in range(2):
        orders = ["AB", "BA"] * (pairs // 2) + (["AB"] if pairs % 2 else [])
        rng.shuffle(orders)
        batches.append(orders)
    return batches
