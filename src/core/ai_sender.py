import base64
import mimetypes
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from google import genai
from google.genai import interactions

from core.claude_bridge import ClaudeBridge
from core.model_catalog import ModelOption
from core.settings import SettingsStore


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


class AISender:
    """Route captured turns while keeping provider conversation state on the Qt thread."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self.settings_store = settings_store or SettingsStore()
        self.api_key = self.settings_store.gemini_key()
        self.client = client or (
            genai.Client(api_key=self.api_key) if self.api_key else None
        )
        self.provider = self.settings_store.provider()
        self.claude_bridge = ClaudeBridge()
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

    def set_models(self, models: tuple[ModelOption, ...]) -> None:
        """Install a discovered catalog on the Qt thread and select an available model.

        Args:
            models (tuple[ModelOption, ...]): The compatible model catalog.
        """
        self.models = {model.model_id: model for model in models}
        if not models:
            self.model = ""
            self.effort_level = "default"
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
        self.contexts = {"gemini": None, "claude": None}
        self.claude_session_id = ""

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
                "Models are not loaded. Open Settings to connect your AI provider."
            )
        self.invalidate_requests()
        return InteractionRequest(
            message,
            tuple(attachments or []),
            self.model,
            self.effort_level,
            self.previous_interaction_id,
            self.generation,
            self.provider,
            self.claude_session_id if self.previous_interaction_id else str(uuid4()),
            self.client,
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
        if request.provider == "claude":
            return self.claude_bridge.send_message(
                request.message,
                request.attachments,
                request.model,
                request.effort_level,
                request.session_id,
                request.previous_interaction_id,
                on_chunk,
                stop_flag,
            )
        client = request.client
        if client is None:
            raise RuntimeError("Add your Gemini API key in Settings first.")
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
                raise RuntimeError(
                    "Could not continue the saved Gemini conversation. Its server-side "
                    "state may be expired or unavailable. Context has not been reset. "
                    "Retry, or use New chat (+) to explicitly start a new conversation. "
                    f"Details: {exc}"
                ) from exc
            raise
