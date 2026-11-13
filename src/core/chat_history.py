import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4


@dataclass
class Conversation:
    """Store one saved chat and the provider context needed to resume it."""

    provider: str
    title: str
    conversation_id: str = field(default_factory=lambda: str(uuid4()))
    updated: float = field(default_factory=time.time)
    messages: list[dict[str, str]] = field(default_factory=list)
    interaction_id: str | None = None
    session_id: str = ""


class ChatHistory:
    """Persist saved conversations as JSON on the Qt thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            path
            or Path(__file__).resolve().parent.parent / "data" / "chat_history.json"
        )
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
        """Remove a conversation and write the history file.

        Args:
            conversation_id (str): The conversation identifier.
        """
        if self.items.pop(conversation_id, None) is not None:
            self._write()

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
