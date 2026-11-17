import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

SCREENSHOT_NAME = re.compile(r"[0-9a-f]{32}\.png")
# Older than any turn still in flight, even in another Que instance
ORPHAN_SECONDS = 86400


@dataclass
class Conversation:
    """Store one saved chat and the provider context needed to resume it."""

    provider: str
    title: str
    conversation_id: str = field(default_factory=lambda: str(uuid4()))
    updated: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)
    interaction_id: str | None = None
    session_id: str = ""


class ChatHistory:
    """Persist saved conversations as JSON on the Qt thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            path
            or Path(__file__).resolve().parent.parent / "data" / "chat_history.json"
        )
        self.screenshots_dir = self.path.parent / "screenshots"
        self.items: dict[str, Conversation] = {}
        try:
            if self.path.exists():
                rows = json.loads(self.path.read_text(encoding="utf-8"))[
                    "conversations"
                ]
                for row in rows:
                    conversation = Conversation(**row)
                    self.items[conversation.conversation_id] = conversation
        except (OSError, ValueError, TypeError, KeyError):
            # Keep the unreadable file instead of overwriting it on the next save
            self.items = {}
            try:
                self.path.replace(self.path.with_suffix(".bak"))
            except OSError:
                pass
        else:
            # Only a fully read history proves which screenshots are unreferenced
            self._prune_screenshots()

    def conversations(self) -> list[Conversation]:
        """Return saved conversations, most recently updated first.

        Returns:
            list[Conversation]: The saved conversations.
        """
        return sorted(self.items.values(), key=lambda item: item.updated, reverse=True)

    def get(self, conversation_id: str) -> Conversation | None:
        """Look up a saved conversation.

        Args:
            conversation_id (str): The conversation identifier.

        Returns:
            Conversation | None: The conversation, or None when it does not exist.
        """
        return self.items.get(conversation_id)

    def save(self, conversation: Conversation) -> None:
        """Insert or update a conversation and write the history file.

        Args:
            conversation (Conversation): The conversation to save.
        """
        self.items[conversation.conversation_id] = conversation
        self._write()

    def delete(self, conversation_id: str) -> None:
        """Remove a conversation and its screenshots, then write the history file.

        Args:
            conversation_id (str): The conversation identifier.
        """
        conversation = self.items.pop(conversation_id, None)
        if conversation is None:
            return
        self._write()
        for message in conversation.messages:
            for path in self.screenshot_paths(message):
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as e:
                    print(f"Error deleting screenshot: {e!s}")

    def keep_screenshots(self, paths: list[str]) -> list[str]:
        """Move sent screenshots into history storage so saved chats can show them.

        Args:
            paths (list[str]): The captured screenshot files.

        Returns:
            list[str]: The stored files, or the original path of any file that could not be moved.
        """
        kept = []
        for path in paths:
            target = self.screenshots_dir / f"{uuid4().hex}.png"
            try:
                self.screenshots_dir.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
            except OSError as e:
                print(f"Error saving screenshot: {e!s}")
                kept.append(path)
            else:
                kept.append(str(target))
        return kept

    def screenshot_names(self, paths: Iterable[str]) -> list[str]:
        """Name the stored screenshots among a turn's attachments for its history entry.

        Args:
            paths (Iterable[str]): The turn's attachment files.

        Returns:
            list[str]: The file names of attachments kept in history storage.
        """
        return [
            Path(path).name
            for path in paths
            if Path(path).parent == self.screenshots_dir
        ]

    def screenshot_paths(self, message: dict[str, Any]) -> tuple[str, ...]:
        """Resolve a saved message's screenshots, ignoring names that are not stored screenshots.

        Args:
            message (dict[str, Any]): The saved message.

        Returns:
            tuple[str, ...]: The screenshot files in the order they were sent.
        """
        return tuple(
            str(self.screenshots_dir / name)
            for name in message.get("screenshots", [])
            if isinstance(name, str) and SCREENSHOT_NAME.fullmatch(name)
        )

    def _prune_screenshots(self) -> None:
        """Delete old stored screenshots that no saved conversation references, e.g. from failed sends."""
        referenced = {
            Path(path).name
            for conversation in self.items.values()
            for message in conversation.messages
            for path in self.screenshot_paths(message)
        }
        now = time.time()
        try:
            for file in self.screenshots_dir.glob("*.png"):
                if (
                    SCREENSHOT_NAME.fullmatch(file.name)
                    and file.name not in referenced
                    and now - file.stat().st_mtime > ORPHAN_SECONDS
                ):
                    file.unlink()
        except OSError as e:
            print(f"Error cleaning screenshots: {e!s}")

    def _write(self) -> None:
        """Atomically replace the history file with the current conversations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"conversations": [asdict(item) for item in self.conversations()]},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
