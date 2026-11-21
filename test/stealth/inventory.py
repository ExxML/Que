"""Discover actions from source and compare them with reviewed coverage decisions."""

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from stealth.results import Result

CONTROL_TYPES = {
    "QPushButton",
    "QToolButton",
    "QCheckBox",
    "QComboBox",
    "QLineEdit",
    "QTextEdit",
    "QPlainTextEdit",
    "QTabBar",
    "QListWidget",
    "QMenu",
    "QAction",
    "QFileDialog",
    "SelectionDropdown",
    "HeaderIconButton",
    "_RecordButton",
    "_ScreenshotButton",
}


def discover(source: Path) -> dict[str, Any]:
    """Collect stable constructor and signal identities without importing application code."""
    controls: dict[str, str] = {}
    connections: set[str] = set()
    control_types = set(CONTROL_TYPES)
    for path in source.rglob("*.py"):
        if "data" in path.relative_to(source).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and (
                any(ast.unparse(base) in control_types for base in node.bases)
                or any(
                    isinstance(method, ast.FunctionDef)
                    and method.name in ("mousePressEvent", "contextMenuEvent")
                    for method in node.body
                )
            ):
                control_types.add(node.name)
    for path in sorted(source.rglob("*.py")):
        if "data" in path.relative_to(source).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        classes.append(
            ast.ClassDef(
                name="module",
                bases=[],
                keywords=[],
                body=[node for node in tree.body if isinstance(node, ast.FunctionDef)],
                decorator_list=[],
            )
        )
        for cls in classes:
            for method in [
                node for node in cls.body if isinstance(node, ast.FunctionDef)
            ]:
                counts: dict[str, int] = {}
                for node in ast.walk(method):
                    if not isinstance(node, ast.Call):
                        continue
                    name = ast.unparse(node.func)
                    prefix = f"{path.relative_to(source).as_posix()}:{cls.name}.{method.name}"
                    if name in control_types:
                        signature = ast.unparse(node).replace("lambda :", "lambda:")
                        counts[signature] = counts.get(signature, 0) + 1
                        digest = hashlib.sha256(signature.encode()).hexdigest()[:10]
                        controls[f"{prefix}/{name}/{digest}/{counts[signature]}"] = (
                            signature
                        )
                    if name.endswith(".connect"):
                        connections.add(
                            f"{prefix}/{ast.unparse(node).replace('lambda :', 'lambda:')}"
                        )
    return {"controls": controls, "connections": sorted(connections)}


def discover_operations(source: Path) -> dict[str, dict[str, Any]]:
    """Inventory every function body, failure handler, branch, and scheduled operation.

    Returns:
        dict[str, dict[str, Any]]: Source locations and semantic hashes requiring coverage decisions.
    """
    operations = {}
    for path in sorted(source.rglob("*.py")):
        if "data" in path.relative_to(source).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        relative = path.relative_to(source).as_posix()
        for node in ast.walk(tree):
            category = (
                "routine"
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else "failure"
                if isinstance(node, ast.ExceptHandler)
                else "branch"
                if isinstance(node, (ast.If, ast.Match))
                else "background"
                if isinstance(node, ast.Call)
                and ast.unparse(node.func).split(".")[-1]
                in {"Thread", "QTimer", "singleShot", "Popen", "run"}
                else None
            )
            if category is None:
                continue
            line = int(getattr(node, "lineno", 0))
            identity = f"{relative}:{line}:{category}"
            operations[identity] = {
                "category": category,
                "source": relative,
                "line": line,
                "expression": ast.unparse(node)
                .splitlines()[0]
                .replace("lambda :", "lambda:"),
                "sha256": hashlib.sha256(_stable_dump(node).encode()).hexdigest(),
            }
    return operations


def compare_operations(source: Path, approved: Path) -> Result:
    """Reject unreviewed code paths without claiming that inventory means execution."""
    current = discover_operations(source)
    baseline = json.loads(approved.read_text(encoding="utf-8"))
    changes = [
        identity
        for identity in current.keys() | baseline.keys()
        if current.get(identity) != baseline.get(identity)
    ]
    return Result(
        "inventory.operations",
        "FAIL" if changes else "PASS",
        "Unreviewed operation changes: " + ", ".join(changes)
        if changes
        else f"Inventory drift check only: {len(current)} routines/branches/failure handlers/background calls. Desktop execution is separately required; this is not coverage proof.",
        evidence=["native/operations.json"],
    )


def compare_inventory(source: Path, approved: Path) -> Result:
    """Fail new or removed controls and connections until their coverage is reviewed."""
    current = discover(source)
    baseline = json.loads(approved.read_text(encoding="utf-8"))
    changes: list[str] = []
    for category in ("controls", "connections"):
        actual = {
            _connection_identity(item) if category == "connections" else item: item
            for item in current[category]
        }
        reviewed = {
            _connection_identity(item) if category == "connections" else item: item
            for item in baseline[category]
        }
        added = actual.keys() - reviewed.keys()
        removed = reviewed.keys() - actual.keys()
        changes.extend(f"{category} added: {actual[item]}" for item in sorted(added))
        changes.extend(
            f"{category} removed: {reviewed[item]}" for item in sorted(removed)
        )
    return Result(
        "inventory.source",
        "FAIL" if changes else "PASS",
        "\n".join(changes)
        or "Every discovered control and connection has a reviewed inventory entry.",
    )


def _connection_identity(connection: str) -> str:
    """Compare a connection's syntax tree while retaining its source and method identity.

    Args:
        connection (str): The inventoried source path, method and connection expression.

    Returns:
        str: An identity unaffected by Python-version differences in ast.unparse formatting.
    """
    source, _, remainder = connection.partition(":")
    method, _, expression = remainder.partition("/")
    return f"{source}:{method}/{_stable_dump(ast.parse(expression, mode='eval'))}"


def _stable_dump(node: Any) -> str:
    """Serialize an AST without the empty fields that only some Python versions include.

    Args:
        node (Any): The AST node, list, or value to serialize.

    Returns:
        str: A representation that is identical on every supported Python version.
    """
    if isinstance(node, ast.AST):
        fields = [
            f"{name}={_stable_dump(value)}"
            for name, value in ast.iter_fields(node)
            if value is not None and value != []
        ]
        return f"{type(node).__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return f"[{', '.join(_stable_dump(item) for item in node)}]"
    return repr(node)
