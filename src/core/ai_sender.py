import base64
import mimetypes
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from dotenv import load_dotenv
from google import genai
from google.genai import interactions


MODELS: list[tuple[str, str]] = [
    ("Gemini 2.5 Flash Lite", "gemini-2.5-flash-lite"),
    ("Gemini 2.5 Flash", "gemini-2.5-flash"),
    ("Gemini 3 Flash Preview", "gemini-3-flash-preview"),
]
DEFAULT_MODEL = MODELS[2][1]


@dataclass(frozen=True)
class InteractionRequest:
    """Capture one turn and its conversation generation before starting a worker."""

    message: str
    attachments: tuple[str, ...]
    model: str
    thinking_enabled: bool
    previous_interaction_id: str | None
    generation: int


class AISender:
    """Stream Gemini interactions with conversation state owned by the Qt thread."""

    def __init__(self) -> None:
        base_dir = Path(__file__).resolve().parent.parent.parent
        load_dotenv(base_dir / ".env")
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.model = DEFAULT_MODEL
        self.thinking_enabled = False
        self.previous_interaction_id: str | None = None
        self.generation = 0

    def set_model(self, model_id: str) -> None:
        """Set the model for subsequent turns without resetting conversation context.

        Args:
            model_id (str): The Gemini model identifier.
        """
        self.model = model_id

    def set_thinking_mode(self, enabled: bool) -> None:
        """Select higher or minimum supported reasoning for subsequent turns.

        Args:
            enabled (bool): Whether to use high reasoning effort.
        """
        self.thinking_enabled = enabled

    def invalidate_requests(self) -> None:
        """Invalidate outstanding requests on the Qt thread without losing context."""
        self.generation += 1

    def reset_chat(self) -> None:
        """Forget the conversation chain and invalidate outstanding requests."""
        self.invalidate_requests()
        self.previous_interaction_id = None

    def prepare_request(
        self, message: str, attachments: list[str] | None = None
    ) -> InteractionRequest:
        """Snapshot a new turn on the Qt thread and supersede older requests.

        Args:
            message (str): The new user message.
            attachments (list[str], optional): Paths to this turn's attachments.

        Returns:
            InteractionRequest: The immutable worker inputs.
        """
        self.invalidate_requests()
        return InteractionRequest(
            message,
            tuple(attachments or []),
            self.model,
            self.thinking_enabled,
            self.previous_interaction_id,
            self.generation,
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
        self.invalidate_requests()
        return True

    def send_message(
        self,
        interaction_request: InteractionRequest,
        on_chunk: Callable[[str], None],
        stop_flag: threading.Event,
    ) -> str | None:
        """Stream a captured turn without modifying shared conversation state.

        Args:
            interaction_request (InteractionRequest): The captured turn and settings.
            on_chunk (Callable[[str], None]): Callback for visible response text.
            stop_flag (threading.Event): The request's cancellation flag.

        Returns:
            str | None: The completed interaction ID, or None when canceled.
        """
        request = interaction_request
        if stop_flag.is_set():
            return None
        parts: list[dict[str, str]] = []
        for filepath in request.attachments:
            mime_type = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
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
        parts.append({"type": "text", "text": request.message})
        config: dict[str, str] = {}
        if request.thinking_enabled:
            config["thinking_level"] = "high"
        elif request.model == "gemini-3-flash-preview":
            config["thinking_level"] = "minimal"
        elif request.model != "gemini-2.5-flash-lite":
            # Interactions exposes levels rather than a zero-token budget.
            config["thinking_level"] = "low"
        if stop_flag.is_set():
            return None
        try:
            stream = self.client.interactions.create(
                model=request.model,
                input=parts,
                previous_interaction_id=request.previous_interaction_id,
                generation_config=config,
                store=True,
                stream=True,
            )
            if isinstance(stream, interactions.Interaction):
                raise RuntimeError("Gemini did not return the requested stream.")
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
                        raise RuntimeError(detail)
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
                raise RuntimeError(
                    "Could not continue the saved Gemini conversation. Its server-side "
                    "state may be expired or unavailable. Context has not been reset. "
                    "Retry, or use Clear to explicitly start a new conversation. "
                    f"Details: {exc}"
                ) from exc
            raise
