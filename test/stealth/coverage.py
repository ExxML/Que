"""Measure actual controlled-fixture execution and expose unexercised source paths."""

import ast
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from stealth.results import Result, atomic_json, read_json


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
    hook_path = directory / "native" / "hook-execution.json"
    if hook_path.exists():
        hits.update(tuple(item) for item in read_json(hook_path)["hits"])
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
                if body and (relative, body[0].lineno) not in hits:
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
        )
    ]
