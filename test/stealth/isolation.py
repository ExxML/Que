"""Stage disposable applications and preserve the user's runtime data and profiles."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from stealth.results import atomic_json


def digest(path: Path) -> str:
    """Hash a file without loading large packaged applications into memory."""
    with path.open("rb") as handle:
        result = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def stage(repository: Path, directory: Path, target: str) -> dict[str, Any]:
    """Copy unchanged code or an entire Nuitka distribution, excluding runtime data."""
    destination = directory / "isolated"
    destination.mkdir()
    source = repository / "src" if target == "source" else Path(target).resolve().parent
    if target != "source" and (
        not Path(target).is_file() or Path(target).suffix.lower() != ".exe"
    ):
        raise ValueError(
            "Packaged target must be an existing .exe; keep standalone dependencies beside it."
        )
    standalone = target != "source" and (
        source.name.endswith(".dist") or any(source.glob("python*.dll"))
    )
    if (target == "source" or standalone) and (
        source == destination or source in destination.parents
    ):
        raise ValueError("Run directory must be outside the target distribution.")
    hashes = {}
    paths = (
        sorted(source.rglob("*"))
        if target == "source" or standalone
        else [Path(target).resolve()]
    )
    for path in paths:
        relative = path.relative_to(source)
        if any(
            part
            in (
                "data",
                "__pycache__",
                ".git",
                ".env",
                ".venv",
                ".mypy_cache",
                ".ruff_cache",
                ".pytest_cache",
            )
            or part.startswith(".env.")
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            raise ValueError(f"Refuse staging symbolic link: {relative}")
        if path.is_file():
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            original = digest(path)
            if digest(output) != original:
                raise OSError(f"Staged copy differs: {relative}")
            hashes[relative.as_posix()] = original
    data = destination / "data"
    data.mkdir(exist_ok=True)
    fixtures = directory / "fixtures"
    fixtures.mkdir()
    for root in (fixtures, data):
        (root / ".que-audit-fixture").write_text(
            "Synthetic Que audit directory\n", encoding="utf-8"
        )
    (fixtures / "document.txt").write_text(
        "QUE-AUDIT-DOCUMENT: synthetic test only. The blue robot counts seven stars.",
        encoding="utf-8",
    )
    (fixtures / "speech.txt").write_text(
        "Microphone: The blue robot counts seven stars.\nLoopback: The green robot counts nine moons.",
        encoding="utf-8",
    )
    config = {
        "claude_directory": str(fixtures.resolve()),
        "context_documents": {},
        "capture_hidden": True,
    }
    atomic_json(data / "config.json", config)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    command = (
        [sys.executable, str(destination / "que.py")]
        if target == "source"
        else [str(destination / Path(target).name)]
    )
    return {
        "package_layout": "source"
        if target == "source"
        else "standalone"
        if standalone
        else "onefile",
        "target": target if target == "source" else Path(target).name,
        "command": command,
        "hashes": hashes,
        "revision": revision.stdout.strip(),
        "executable_hash": None if target == "source" else digest(Path(target)),
        "release_validation": target != "source",
        "claude_retention": "Fresh synthetic sessions may be retained by Claude through its normal transcript behavior.",
    }


def child_environment(directory: Path) -> dict[str, str]:
    """Allow necessary OS settings and isolate caches without copying unrelated secrets."""
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "USERPROFILE",
        "USERNAME",
        "APPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "HOMEDRIVE",
        "HOMEPATH",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    for key, folder in (
        ("LOCALAPPDATA", "local"),
        ("TEMP", "temp"),
        ("TMP", "temp"),
        ("HF_HOME", "hf"),
    ):
        path = directory / "private" / folder
        path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path.resolve())
    environment["PYTHONUTF8"] = "1"
    return environment


def supply_gemini_credential(repository: Path, directory: Path) -> bool:
    """Copy only the encrypted Gemini credential, or encrypt the explicit test key."""
    from core.settings import SettingsStore

    isolated = SettingsStore(directory / "isolated" / "data" / "config.json")
    key = os.environ.get("QUE_AUDIT_GEMINI_KEY")
    if key is None:
        key = SettingsStore(repository / "src" / "data" / "config.json").gemini_key()
    if key:
        isolated.save("gemini", "", "default", key)
    return bool(key)
