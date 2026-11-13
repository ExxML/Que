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

from core.model_catalog import ModelOption


@dataclass(frozen=True)
class InteractionRequest:
    """Capture one turn and its conversation generation before starting a worker."""

    message: str
    attachments: tuple[str, ...]
    model: str
    effort_level: str
    previous_interaction_id: str | None
    generation: int


class AISender:
    """Stream Gemini interactions with conversation state owned by the Qt thread."""

    def __init__(self) -> None:
        base_dir = Path(__file__).resolve().parent.parent.parent
        load_dotenv(base_dir / ".env")
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.models: dict[str, ModelOption] = {}
        self.model = ""
        self.effort_level = "default"
        self.previous_interaction_id: str | None = None
        self.generation = 0

    def set_models(self, models: tuple[ModelOption, ...]) -> None:
        """Install a discovered catalog on the Qt thread and select an available model.

        Args:
            models (tuple[ModelOption, ...]): The compatible model catalog.
        """
        self.models = {model.model_id: model for model in models}
        if self.model not in self.models:
            self.set_model(models[0].model_id)

    def set_model(self, model_id: str) -> None:
        """Set the model for subsequent turns without resetting conversation context.

        Args:
            model_id (str): The Gemini model identifier.
        """
        levels = self.models[model_id].effort_levels
        self.model = model_id
        if self.effort_level not in levels:
            self.effort_level = levels[0]

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
        if not self.model:
            raise RuntimeError(
                "Gemini models are not loaded. Retry model discovery first."
            )
        self.invalidate_requests()
        return InteractionRequest(
            message,
            tuple(attachments or []),
            self.model,
            self.effort_level,
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
        if stop_flag.is_set():
            return None
        try:
            stream = self.client.interactions.create(
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
