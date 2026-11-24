import base64
import mimetypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import cast
from uuid import uuid4

from google import genai
from google.genai import interactions

from core.claude_bridge import ClaudeBridge, ClaudeTurn
from core.document_text import extract_text
from core.model_catalog import ModelOption
from core.prompts import (
    BROWSABLE_FOLDER,
    CLOSED_FOLDER,
    FACT_SHEET_FIRST,
    FACT_SHEET_FIRST_STALE,
    MISSING_DETAIL,
    STALE_FACT_SHEET,
    documents_block,
    fact_sheet_detail,
    instructions_block,
    job_details_block,
    readme_detail,
    resume_details_block,
    transcript_block,
    unreadable_detail,
)
from core.resume_facts import facts_directory, load_sheet
from core.settings import CONTEXT_DOCUMENTS, SettingsStore


@dataclass(frozen=True)
class InteractionRequest:
    """Capture one turn and its conversation generation before starting a worker."""

    message: str
    attachments: tuple[str, ...]
    model: str
    effort_level: str
    previous_interaction_id: str | None
    generation: int
    provider: str = "gemini"
    session_id: str = ""
    client: genai.Client | None = None
    claude_directory: Path | None = None
    transcript: str = ""
    transcript_seq: int = 0
    context: str = ""
    resume_details: tuple[tuple[str, str], ...] = ()
    display: str = ""  # Shown and saved in the chat instead of the message; never sent
    facts_directory: Path | None = (
        None  # The cache of fact sheets about linked resume folders
    )

    @property
    def prompt(self) -> str:
        """Return the message with any instruction, documents, and new audio transcript prepended as context.

        A new conversation also reads each linked resume detail from disk, so call this off the Qt thread.

        Returns:
            str: The text sent to the provider.
        """
        prompt = self.message
        if self.transcript:
            prompt = transcript_block(self.transcript, prompt)
        # Documents lead so new conversations share a prefix the provider can cache
        context = self.context
        if self.previous_interaction_id is None and self.resume_details:
            details = _resume_details_context(
                self.resume_details, self.provider == "claude", self.facts_directory
            )
            context = f"{context}\n\n{details}" if context else details
        return f"{context}\n\n{prompt}" if context else prompt

    @property
    def folders(self) -> tuple[Path, ...]:
        """Return the linked resume folders that Claude may read on every turn.

        Returns:
            tuple[Path, ...]: The linked paths that are existing folders.
        """
        return tuple(
            Path(path) for _name, path in self.resume_details if Path(path).is_dir()
        )


class AISender:
    """Route captured turns while keeping provider conversation state on the Qt thread."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        client: genai.Client | None = None,
        live: bool = False,
    ) -> None:
        self.settings_store = settings_store or SettingsStore()
        self.api_key = self.settings_store.gemini_key()
        self.client = client or (
            genai.Client(api_key=self.api_key) if self.api_key else None
        )
        self.provider = self.settings_store.provider()
        self.claude_bridge = ClaudeBridge(live=live)
        self.facts_directory = facts_directory(self.settings_store)
        self.next_session_id = str(
            uuid4()
        )  # Known ahead so a new chat's CLI can start early
        saved_directory = self.settings_store.claude_directory()
        self.claude_directory = Path(saved_directory) if saved_directory else None
        self.contexts: dict[str, str | None] = {"gemini": None, "claude": None}
        self.claude_session_id = ""
        self.models: dict[str, ModelOption] = {}
        self.model = ""
        self.effort_level = "default"
        self.previous_interaction_id: str | None = None
        self.generation = 0

    def configure(self, provider: str, api_key: str) -> None:
        """Apply provider credentials without mixing conversation identifiers.

        Args:
            provider (str): The chosen provider.
            api_key (str): The saved Gemini credential.
        """
        if provider not in ("gemini", "claude"):
            raise ValueError("Unknown AI provider.")
        self.invalidate_requests()
        self.contexts[self.provider] = self.previous_interaction_id
        if self.api_key != api_key:
            self.client = genai.Client(api_key=api_key) if api_key else None
            self.api_key = api_key
            self.contexts["gemini"] = None
        self.provider = provider
        self.previous_interaction_id = self.contexts[provider]
        self.models = {}
        self.model = ""
        self.effort_level = "default"
        self.prewarm()

    def set_models(self, models: tuple[ModelOption, ...]) -> None:
        """Install a discovered catalog on the Qt thread and select an available model.

        Args:
            models (tuple[ModelOption, ...]): The compatible model catalog.
        """
        self.models = {model.model_id: model for model in models}
        if not models:
            self.model = ""
            self.effort_level = "default"
            self.prewarm()
        elif self.model not in self.models:
            self.set_model(models[0].model_id)
        else:
            self.set_model(self.model)

    def set_model(self, model_id: str) -> None:
        """Set the model for subsequent turns without resetting conversation context.

        Args:
            model_id (str): The Gemini model identifier.
        """
        levels = self.models[model_id].effort_levels
        self.model = model_id
        if self.effort_level not in levels:
            self.effort_level = levels[0]
        self.prewarm()

    def set_effort_level(self, effort_level: str) -> None:
        """Select a supported reasoning effort for subsequent turns.

        Args:
            effort_level (str): The reasoning level supported by the active model.
        """
        if effort_level not in self.models[self.model].effort_levels:
            raise ValueError(
                f"Unsupported effort level for {self.model}: {effort_level}"
            )
        self.effort_level = effort_level
        self.prewarm()

    def prewarm(self) -> None:
        """Start Claude's CLI for the next turn ahead of time, or close it when the next turn is not Claude's."""
        if self.provider != "claude" or not self.model:
            self.claude_bridge.prewarm(None)
            return
        checkpoint = self.previous_interaction_id
        self.claude_bridge.prewarm(
            ClaudeTurn(
                self.model,
                self.effort_level,
                self.claude_session_id if checkpoint else self.next_session_id,
                checkpoint,
                self.claude_directory,
                tuple(
                    Path(path)
                    for _name, path in self.settings_store.resume_details()
                    if Path(path).is_dir()
                ),
            )
        )

    def invalidate_requests(self) -> None:
        """Invalidate outstanding requests on the Qt thread without losing context."""
        self.generation += 1

    def reset_chat(self) -> None:
        """Forget the conversation chain and invalidate outstanding requests."""
        self.invalidate_requests()
        self.previous_interaction_id = None
        self.contexts = {"gemini": None, "claude": None}
        self.claude_session_id = ""
        self.prewarm()

    def restore_chat(self, interaction_id: str | None, session_id: str) -> None:
        """Resume a saved conversation of the active provider.

        Args:
            interaction_id (str | None): The last accepted server interaction ID.
            session_id (str): The saved Claude session UUID, or an empty string for Gemini.
        """
        self.reset_chat()
        self.previous_interaction_id = interaction_id
        self.contexts[self.provider] = interaction_id
        self.claude_session_id = session_id
        self.prewarm()

    def prepare_request(
        self,
        message: str,
        attachments: list[str] | None = None,
        transcript: str = "",
        transcript_seq: int = 0,
        display: str = "",
    ) -> InteractionRequest:
        """Snapshot a new turn on the Qt thread and supersede older requests.

        Args:
            message (str): The new user message.
            attachments (list[str], optional): Paths to this turn's attachments.
            transcript (str): Audio transcript recorded since the last committed turn.
            transcript_seq (int): The last transcript segment included.
            display (str): Text shown and saved in the chat instead of the message, if any.

        Returns:
            InteractionRequest: The immutable worker inputs.
        """
        if not self.model:
            raise RuntimeError(
                "Models are not loaded. Open Settings to connect your AI provider."
            )
        self.invalidate_requests()
        session_id = self.claude_session_id
        if not self.previous_interaction_id:
            # A retried first turn never resumes the session of an uncommitted one
            session_id, self.next_session_id = self.next_session_id, str(uuid4())
        return InteractionRequest(
            message,
            tuple(attachments or []),
            self.model,
            self.effort_level,
            self.previous_interaction_id,
            self.generation,
            self.provider,
            session_id,
            self.client,
            self.claude_directory,
            transcript,
            transcript_seq,
            # Providers keep earlier turns, so the instruction and documents are sent once per conversation
            "" if self.previous_interaction_id else self._context(),
            # Snapshotted every turn because Claude needs folder access after the first
            tuple(self.settings_store.resume_details()),
            display,
            self.facts_directory,
        )

    def commit_interaction(
        self, interaction_request: InteractionRequest, interaction_id: str
    ) -> bool:
        """Accept a successful current turn on the Qt thread.

        Args:
            interaction_request (InteractionRequest): The completed request snapshot.
            interaction_id (str): The completed server interaction identifier.

        Returns:
            bool: Whether this result advanced the conversation.
        """
        if not interaction_id or interaction_request.generation != self.generation:
            return False
        self.previous_interaction_id = interaction_id
        self.contexts[interaction_request.provider] = interaction_id
        if interaction_request.provider == "claude":
            self.claude_session_id = interaction_request.session_id
        self.invalidate_requests()
        self.prewarm()
        return True

    def send_message(
        self,
        interaction_request: InteractionRequest,
        on_chunk: Callable[[str], None],
        stop_flag: threading.Event,
        on_status: Callable[[str], None] | None = None,
    ) -> str | None:
        """Stream a captured turn without modifying shared conversation state.

        Args:
            interaction_request (InteractionRequest): The captured turn and settings.
            on_chunk (Callable[[str], None]): Callback for visible response text.
            stop_flag (threading.Event): The request's cancellation flag.
            on_status (Callable[[str], None], optional): Callback describing Claude's tool use.

        Returns:
            str | None: The completed interaction ID, or None when canceled.
        """
        request = interaction_request
        if stop_flag.is_set():
            return None
        if request.provider == "claude":
            return self.claude_bridge.send_message(
                request.prompt,
                request.attachments,
                request.model,
                request.effort_level,
                request.session_id,
                request.previous_interaction_id,
                request.claude_directory,
                on_chunk,
                stop_flag,
                request.folders,
                on_status,
            )
        client = request.client
        if client is None:
            raise RuntimeError("Add your Gemini API key in Settings first.")
        parts: list[dict[str, str]] = []
        for filepath in request.attachments:
            mime_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
            if mime_type.startswith("text/"):
                parts.append(
                    {
                        "type": "text",
                        "text": Path(filepath).read_text(
                            encoding="utf-8", errors="replace"
                        ),
                    }
                )
                continue
            kind = mime_type.split("/")[0]
            if kind not in ("image", "audio", "video"):
                kind = "document"
            parts.append(
                {
                    "type": kind,
                    "mime_type": mime_type,
                    "data": base64.b64encode(Path(filepath).read_bytes()).decode(
                        "ascii"
                    ),
                }
            )
        parts.append({"type": "text", "text": request.prompt})
        if stop_flag.is_set():
            return None
        try:
            stream = client.interactions.create(
                model=request.model,
                input=parts,
                previous_interaction_id=request.previous_interaction_id,
                generation_config=(
                    {}
                    if request.effort_level == "default"
                    else {"thinking_level": request.effort_level}
                ),
                store=True,
                stream=True,
            )
            if isinstance(stream, interactions.Interaction):
                raise RuntimeError("Gemini did not return the requested stream.")  # noqa: TRY004
            completed_id: str | None = None
            try:
                for event in stream:
                    if stop_flag.is_set():
                        return None
                    if isinstance(event, interactions.StepDelta):
                        if (
                            isinstance(event.delta, interactions.TextDelta)
                            and event.delta.text
                        ):
                            on_chunk(event.delta.text)
                    elif isinstance(event, interactions.ErrorEvent):
                        detail = (
                            event.error.message
                            if event.error
                            else "Unknown streaming error."
                        )
                        raise RuntimeError(detail)  # noqa: TRY004
                    elif isinstance(event, interactions.InteractionCompletedEvent):
                        if (
                            event.interaction.status != "completed"
                            or not event.interaction.id
                        ):
                            raise RuntimeError(
                                "Gemini returned an unsuccessful interaction."
                            )
                        completed_id = event.interaction.id
            finally:
                cast(Callable[[], None], stream.close)()
            if stop_flag.is_set():
                return None
            if completed_id is None:
                raise RuntimeError("Gemini stream ended without successful completion.")
            return completed_id
        except Exception as exc:
            if stop_flag.is_set():
                return None
            if request.previous_interaction_id:
                context_error = (
                    "The saved interaction is expired or unavailable. "
                    if getattr(exc, "status_code", getattr(exc, "code", None)) == 404
                    and "interaction" in str(exc).lower()
                    else ""
                )
                raise RuntimeError(
                    "Gemini could not complete this request. Context has not been reset. "
                    f"{context_error}Check the provider error below before retrying. "
                    f"Details: {exc}"
                ) from exc
            raise

    def _context(self) -> str:
        """Wrap the user's saved instruction, job details and documents as background for a new conversation.

        Returns:
            str: The instruction, job details and documents, or an empty string when none is saved.
        """
        parts: list[str] = []
        instruction = self.settings_store.instruction()
        if instruction:
            parts.append(instructions_block(instruction))
        job_details = self.settings_store.job_details()
        if job_details:
            parts.append(job_details_block(job_details))
        documents = self.settings_store.context_documents()
        sections = [
            f"<{kind}>\n{documents[kind][1]}\n</{kind}>"
            for kind in CONTEXT_DOCUMENTS
            if kind in documents
        ]
        if sections:
            parts.append(documents_block("\n".join(sections)))
        return "\n\n".join(parts)


def _resume_details_context(
    details: tuple[tuple[str, str], ...],
    browse: bool,
    facts_directory: Path | None = None,
) -> str:
    """Read each linked resume detail fresh so edits to its notes apply to the next new chat.

    Args:
        details (tuple[tuple[str, str], ...]): The item name and file or folder path of each link.
        browse (bool): Whether the provider can open linked folders with file tools.
        facts_directory (Path | None): The cache of fact sheets about linked folders, if any.

    Returns:
        str: The details wrapped as background for a new conversation.
    """
    sections = []
    for name, path in details:
        source = Path(path)
        sheet = (
            load_sheet(facts_directory, source)
            if facts_directory is not None and source.is_dir()
            else None
        )
        if not source.exists():
            body = MISSING_DETAIL
        elif sheet is not None:
            body = _fact_sheet_body(*sheet, browse)
        elif source.is_dir() and browse:
            body = BROWSABLE_FOLDER
        elif source.is_dir():
            readme = next(
                (
                    source / readme_name
                    for readme_name in ("README.md", "README.txt")
                    if (source / readme_name).is_file()
                ),
                None,
            )
            body = readme_detail(_read_detail(readme)) if readme else CLOSED_FOLDER
        else:
            body = _read_detail(source)
        sections.append(
            f'<detail name="{escape(name or source.name)}" path="{escape(path)}">\n{body}\n</detail>'
        )
    return resume_details_block("\n".join(sections))


def _fact_sheet_body(text: str, current: bool, browse: bool) -> str:
    """Present a linked folder's fact sheet, warning when the folder changed after it was written.

    Args:
        text (str): The fact sheet.
        current (bool): Whether the folder is unchanged since the sheet was written.
        browse (bool): Whether the provider can open the folder with file tools.

    Returns:
        str: The detail body sent to the provider.
    """
    body = fact_sheet_detail(text)
    if not current:
        body = f"{STALE_FACT_SHEET} {body}"
    if browse:
        body += f"\n\n{FACT_SHEET_FIRST}{'.' if current else FACT_SHEET_FIRST_STALE}"
    return body


def _read_detail(path: Path) -> str:
    """Extract a linked file's text, or explain why it cannot be used.

    Args:
        path (Path): The linked file.

    Returns:
        str: The file text, or a note that it is unavailable.
    """
    try:
        return extract_text(path)
    except ValueError as error:
        return unreadable_detail(str(error))
