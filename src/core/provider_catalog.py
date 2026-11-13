import threading
from dataclasses import dataclass

from google import genai
from google.genai import types
from PyQt6.QtCore import QObject, pyqtSignal

from core.claude_bridge import ClaudeBridge
from core.gemini_efforts import load_efforts
from core.model_catalog import ModelOption, compatible_model


@dataclass(frozen=True)
class ProviderResult:
    """Carry one provider discovery result back to the Qt thread."""

    provider: str
    models: tuple[ModelOption, ...]
    status: str


class ProviderCatalog(QObject):
    """Check connections asynchronously and reject stale provider results."""

    completed = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)

    def __init__(self, claude_bridge: ClaudeBridge) -> None:
        super().__init__()
        self.claude_bridge = claude_bridge
        self.revision = 0

    def refresh(
        self, provider: str, api_key: str, test_generation: bool = False
    ) -> int:
        """Start a connection check with immutable settings.

        Args:
            provider (str): The provider to discover.
            api_key (str): The Gemini credential to check.
            test_generation (bool): Also send a minimal Gemini generation request.

        Returns:
            int: The request revision used to discard stale results.
        """
        self.revision += 1
        revision = self.revision
        threading.Thread(
            target=self._load,
            args=(revision, provider, api_key, test_generation),
            daemon=True,
        ).start()
        return revision

    def _load(
        self, revision: int, provider: str, api_key: str, test_generation: bool
    ) -> None:
        """Retrieve provider capabilities without touching UI objects.

        Args:
            revision (int): The discovery request identifier.
            provider (str): The provider to check.
            api_key (str): The captured Gemini key.
            test_generation (bool): Whether to verify Gemini generation access.
        """
        try:
            if provider == "claude":
                models, status = self.claude_bridge.discover()
            else:
                if not api_key.strip():
                    raise ValueError(
                        "Paste your Gemini API key to load available models."
                    )
                with genai.Client(
                    api_key=api_key, http_options=types.HttpOptions(timeout=20000)
                ) as client:
                    efforts = load_efforts()
                    options = [
                        compatible_model(model, efforts)
                        for model in client.models.list(
                            config=types.ListModelsConfig(page_size=100)
                        )
                    ]
                    models = tuple(
                        sorted(
                            (option for option in options if option is not None),
                            key=lambda option: option.display_name,
                        )
                    )
                    if not models:
                        raise RuntimeError(
                            "This key has no compatible Gemini chat models."
                        )
                    status = "Key accepted · Models loaded. Test key to verify generation access."
                    if test_generation:
                        client.interactions.create(
                            model=models[0].model_id,
                            input="Reply OK.",
                            generation_config={"max_output_tokens": 16},
                            store=False,
                        )
                        status = "Healthy · Gemini accepted a test generation request."
                    if not efforts:
                        status += (
                            " Effort capabilities unavailable; using model defaults."
                        )
            self.completed.emit(revision, ProviderResult(provider, models, status))
        except Exception as exc:
            message = str(exc)
            if api_key:
                message = message.replace(api_key, "[API key]")
            self.failed.emit(revision, message)
