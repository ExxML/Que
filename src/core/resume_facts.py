import hashlib
import json
import os
import queue
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile

from PyQt6.QtCore import QObject, pyqtSignal

from core.claude_bridge import ClaudeBridge
from core.prompts import fact_sheet_request

WORKERS = 3  # Fact sheets written at once


class ResumeFacts(QObject):
    """Write a fact sheet for each linked resume folder, so new chats rarely need file tools."""

    progress = (
        pyqtSignal()
    )  # A fact sheet was written or failed; read status() for the counts

    def __init__(self, claude_bridge: ClaudeBridge, directory: Path) -> None:
        super().__init__()
        self.claude_bridge = claude_bridge
        self.directory = directory
        self.lock = threading.Lock()
        self.pending: queue.Queue[tuple[str, Path, str, str]] = queue.Queue()
        self.queued: set[Path] = set()
        self.workers = 0
        self.errors: dict[Path, str] = {}

    def refresh(
        self,
        details: list[tuple[str, str]],
        model: str,
        effort: str,
        rewrite: bool = False,
    ) -> None:
        """Write fact sheets for linked folders that have none or changed since theirs was written.

        Args:
            details (list[tuple[str, str]]): The item name and file or folder path of each link.
            model (str): The Claude model that writes the sheets, or an empty string for the CLI's default.
            effort (str): The advertised effort or default.
            rewrite (bool): Whether to also rewrite sheets that are up to date.
        """
        if not self.claude_bridge.live:
            return
        with self.lock:
            for name, path in details:
                folder = Path(path)
                sheet = load_sheet(self.directory, folder)
                current = not rewrite and bool(sheet and sheet[1])
                if folder in self.queued or not folder.is_dir() or current:
                    continue
                self.queued.add(folder)
                self.errors.pop(folder, None)
                self.pending.put((name, folder, model, effort))
            starting = max(0, min(WORKERS, len(self.queued)) - self.workers)
            self.workers += starting
        for _ in range(starting):
            threading.Thread(target=self._work, daemon=True).start()

    def status(self, details: list[tuple[str, str]]) -> tuple[int, int, int, str]:
        """Count the linked folders whose fact sheet is current, being written, or failed.

        Args:
            details (list[tuple[str, str]]): The item name and file or folder path of each link.

        Returns:
            tuple[int, int, int, str]: Current sheets, linked folders, sheets being written, and the latest error.
        """
        folders = [Path(path) for _name, path in details if Path(path).is_dir()]
        current = 0
        for folder in folders:
            sheet = load_sheet(self.directory, folder)
            current += bool(sheet and sheet[1])
        with self.lock:
            writing = len(self.queued & set(folders))
            errors = [
                self.errors[folder] for folder in folders if folder in self.errors
            ]
        return current, len(folders), writing, errors[-1] if errors else ""

    def state(self, folder: Path) -> tuple[str, str]:
        """Describe one linked folder's fact sheet.

        Args:
            folder (Path): The linked folder.

        Returns:
            tuple[str, str]: "writing", "failed", "current", "outdated" or "none", and the failure message.
        """
        with self.lock:
            if folder in self.queued:
                return "writing", ""
            if folder in self.errors:
                return "failed", self.errors[folder]
        sheet = load_sheet(self.directory, folder)
        if sheet is None:
            return "none", ""
        return "current" if sheet[1] else "outdated", ""

    def _work(self) -> None:
        """Write queued fact sheets until none are left."""
        while True:
            with self.lock:
                try:
                    name, folder, model, effort = self.pending.get_nowait()
                except queue.Empty:
                    self.workers -= 1
                    return
            try:
                self._write(name, folder, model, effort)
            except (OSError, RuntimeError, ValueError) as error:
                with self.lock:
                    self.errors[folder] = f"{folder.name}: {error}"
            with self.lock:
                self.queued.discard(folder)
            self.progress.emit()

    def _write(self, name: str, folder: Path, model: str, effort: str) -> None:
        """Ask Claude for one folder's fact sheet and save it with the folder's current fingerprint.

        Args:
            name (str): The resume item linked to the folder.
            folder (Path): The linked folder.
            model (str): The Claude model, or an empty string for the CLI's default.
            effort (str): The advertised effort or default.
        """
        # Fingerprint first, so a change made while Claude reads marks the sheet out of date
        stamp = fingerprint(folder)
        text = self.claude_bridge.ask(
            fact_sheet_request(name or folder.name),
            model,
            effort,
            folder,
            threading.Event(),
        ).strip()
        if not text:
            raise RuntimeError("Claude returned an empty fact sheet.")
        save_sheet(
            self.directory, {"path": str(folder), "fingerprint": stamp, "text": text}
        )


def load_sheet(directory: Path, folder: Path) -> tuple[str, bool] | None:
    """Read a folder's saved fact sheet and whether the folder is unchanged since it was written.

    Args:
        directory (Path): The fact sheet cache.
        folder (Path): The linked folder.

    Returns:
        tuple[str, bool] | None: The sheet and whether it is current, or None when there is none.
    """
    try:
        data = json.loads(_sheet_path(directory, folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    return data["text"], data.get("fingerprint") == fingerprint(folder)


def sheet_record(directory: Path, folder: Path) -> dict[str, str] | None:
    """Read a folder's saved fact sheet exactly as stored, for a settings backup.

    Args:
        directory (Path): The fact sheet cache.
        folder (Path): The linked folder.

    Returns:
        dict[str, str] | None: The folder path, fingerprint and text, or None when there is no valid sheet.
    """
    try:
        data = json.loads(_sheet_path(directory, folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(
        isinstance(data.get(field), str) for field in ("fingerprint", "text")
    ):
        return None
    return {
        "path": str(folder),
        "fingerprint": data["fingerprint"],
        "text": data["text"],
    }


def save_sheet(directory: Path, record: dict[str, str]) -> None:
    """Atomically save a fact sheet record under its folder's cache file.

    Args:
        directory (Path): The fact sheet cache.
        record (dict[str, str]): The folder path, fingerprint and text.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
    ) as file:
        json.dump(record, file)
    os.replace(file.name, _sheet_path(directory, Path(record["path"])))


def fingerprint(folder: Path) -> str:
    """Identify a folder's state cheaply: its git commit, or else its newest top-level change.

    Args:
        folder (Path): The linked folder.

    Returns:
        str: A value that changes when the folder's contents likely changed.
    """
    for root in (folder, *folder.parents):
        if (root / ".git").exists():
            commit = _git_commit(root / ".git")
            if commit:
                return f"git:{commit}"
            break
    try:
        newest = max(
            [folder.stat().st_mtime_ns]
            + [entry.stat().st_mtime_ns for entry in os.scandir(folder)]
        )
    except OSError:
        return ""
    return f"mtime:{newest}"


def _git_commit(git: Path) -> str:
    """Read the checked-out commit of a repository without running git.

    Args:
        git (Path): The repository's .git folder, or a worktree's .git file.

    Returns:
        str: The commit hash, or an empty string when it cannot be read.
    """
    try:
        if git.is_file():  # A worktree or submodule points at its real git folder
            pointer = git.read_text(encoding="utf-8").strip()
            git = (git.parent / pointer.removeprefix("gitdir:").strip()).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head.removeprefix("ref:").strip()
        common = (
            git  # A worktree keeps its branches in the main repository's git folder
        )
        if (git / "commondir").is_file():
            common = (
                git / (git / "commondir").read_text(encoding="utf-8").strip()
            ).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()
            packed = base / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        return line.split(" ", 1)[0]
    except OSError:
        pass
    return ""


def _sheet_path(directory: Path, folder: Path) -> Path:
    """Return where a folder's fact sheet is cached.

    Args:
        directory (Path): The fact sheet cache.
        folder (Path): The linked folder.

    Returns:
        Path: The cache file, named by a hash of the folder's path.
    """
    key = os.path.normcase(os.path.abspath(folder))
    return directory / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}.json"
