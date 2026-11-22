"""Measure actual controlled-fixture execution and expose unexercised source paths."""

import ast
import sys
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from stealth.results import Result, atomic_json, read_json

KNOWN_GAPS = Path(__file__).with_name(
    "known-execution-gaps.json"
)  # Accepted gaps; may only shrink.


class SourceCoverage:
    """Collect source line hits without modifying the application or its release build."""

    def __init__(self, source: Path) -> None:
        self.source = source.resolve()
        self.paths = {
            str(path.resolve()).lower(): path.relative_to(self.source).as_posix()
            for path in self.source.rglob("*.py")
        }
        self.hits: set[tuple[str, int]] = set()

    def start(self) -> None:
        """Observe this thread and future test worker threads."""
        sys.settrace(self._trace)
        threading.settrace(self._trace)

    def stop(self, path: Path) -> None:
        """Persist raw executed lines and stop observing new calls."""
        sys.settrace(None)
        threading.settrace(None)
        atomic_json(
            path,
            {
                "scope": "Instrumented offline fixtures only; not unchanged desktop behavior.",
                "hits": sorted(self.hits),
            },
        )

    def _trace(
        self, frame: FrameType, event: str, _argument: Any
    ) -> Callable[..., Any] | None:
        """Record application lines and skip tracing unrelated library frames."""
        relative = self.paths.get(frame.f_code.co_filename.lower())
        if relative is None:
            return None
        if event == "line":
            self.hits.add((relative, frame.f_lineno))
        return self._trace


def coverage_results(source: Path, directory: Path) -> list[Result]:
    """Report every unexercised routine, failure body, and conditional arm as incomplete."""
    path = directory / "native" / "source-execution.json"
    if not path.exists():
        return [
            Result(
                "coverage.execution",
                "INCONCLUSIVE",
                "Source execution evidence is missing.",
            )
        ]
    hits = {tuple(item) for item in read_json(path)["hits"]}
    # Hook callbacks and subprocess fixtures run through test/stealth/traced.py record their own hits.
    for extra in [
        directory / "native" / "hook-execution.json",
        *(directory / "native").glob("source-execution-*.json"),
    ]:
        if extra.exists():
            hits.update(tuple(item) for item in read_json(extra)["hits"])
    gaps = []
    for file in source.rglob("*.py"):
        relative = file.relative_to(source).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            bodies = []
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ExceptHandler)
            ):
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body = body[1:]
                bodies = [body]
            elif isinstance(node, ast.If):
                bodies = [node.body, node.orelse]
            for body in bodies:
                # A multi-line first statement reports its line event on a later line of its span.
                if body and not any(
                    (relative, line) in hits
                    for line in range(
                        body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1
                    )
                ):
                    gaps.append(
                        {
                            "source": relative,
                            "line": body[0].lineno,
                            "kind": type(node).__name__,
                            "trigger": ast.unparse(node).splitlines()[0],
                        }
                    )
    atomic_json(directory / "native" / "execution-gaps.json", gaps)
    return [
        Result(
            "coverage.execution",
            "INCONCLUSIVE" if gaps else "PASS",
            f"{len(gaps)} source entry/branch/failure bodies lack offline execution evidence. Line hits establish execution only, not browser protection or complete condition coverage.",
            evidence=["native/source-execution.json", "native/execution-gaps.json"],
        ),
        ratchet_result(gaps, read_json(KNOWN_GAPS) if KNOWN_GAPS.exists() else []),
    ]


def gap_key(gap: dict[str, Any]) -> str:
    """Identify a gap by file, kind and opening line, so unrelated edits that move lines keep it known.

    Args:
        gap (dict[str, Any]): A gap from execution-gaps.json.

    Returns:
        str: The key.
    """
    return f"{gap['source']} | {gap['kind']} | {gap['trigger']}"


def ratchet_result(gaps: list[dict[str, Any]], known: list[str]) -> Result:
    """Fail new unexecuted paths, and known gaps that tests now cover, so the known list only shrinks.

    Args:
        gaps (list[dict[str, Any]]): The gaps measured in this run.
        known (list[str]): The accepted gap keys from known-execution-gaps.json.

    Returns:
        Result: PASS when the measured gaps match the known list exactly.
    """
    measured, accepted = Counter(map(gap_key, gaps)), Counter(known)
    new, covered = (
        sorted((measured - accepted).elements()),
        sorted((accepted - measured).elements()),
    )
    problems = []
    if new:
        problems.append(
            f"New code that no offline test executes (add tests): {'; '.join(new)}."
        )
    if covered:
        problems.append(
            f"Known gaps now executed; remove them with `python test/stealth/approve.py prune-gaps RUN`: {'; '.join(covered)}."
        )
    return Result(
        "coverage.ratchet",
        "FAIL" if problems else "PASS",
        " ".join(problems)
        or f"No new unexecuted code; {len(known)} known gaps remain to be covered.",
        evidence=["native/execution-gaps.json"],
    )
