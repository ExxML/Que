import re
import threading
from dataclasses import dataclass

from google import genai
from google.genai import types
from PyQt6.QtCore import QObject, pyqtSignal

from core.gemini_efforts import load_efforts

# Every level the installed SDK can send, offered unverified when the docs table is unreadable.
API_EFFORTS = tuple(
    level.value.lower()
    for level in types.ThinkingLevel
    if level is not types.ThinkingLevel.THINKING_LEVEL_UNSPECIFIED
)
EFFORT_WARNING = "Could not read Gemini's supported effort levels from its docs, so every level is offered. Gemini rejects levels a model does not support."


@dataclass(frozen=True)
class ModelOption:
    """Describe a discovered model compatible with Que's interaction pipeline."""

    model_id: str
    display_name: str
    effort_levels: tuple[str, ...]
    effort_warning: str = ""


def compatible_model(
    model: types.Model, efforts: dict[str, tuple[str, ...]] | None = None
) -> ModelOption | None:
    """Filter discovered models using the documented multimodal chat families.

    The Models API does not expose input/output modalities or Interactions support.
    Accept only standard Gemini 2.5+ chat variants; exclude specialized and unknown
    variants rather than inferring capabilities from generateContent alone.

    Args:
        model (types.Model): Metadata returned by Gemini's Models API.
        efforts (dict[str, tuple[str, ...]] | None): Published effort capabilities; empty
            offers every API level with a warning to thinking models.

    Returns:
        ModelOption | None: A compatible model, or None for an unsupported variant.
    """
    model_id = (model.name or "").removeprefix("models/")
    match = re.fullmatch(
        r"gemini-(\d+)(?:\.(\d+))?-(flash(?:-lite)?|pro|carbon|argon|hydrogen)"
        r"(?:-(?:preview|exp)(?:-\d+(?:-\d+)*)?|-\d{3}|-latest)?",
        model_id,
    )
    if match is None or not {"generateContent", "createInteraction"}.intersection(
        model.supported_actions or []
    ):
        return None
    version = (int(match[1]), int(match[2] or 0))
    if version < (2, 5):
        return None
    levels: tuple[str, ...] = ("default",)
    warning = ""
    if model.thinking and not efforts:
        levels = ("default", *API_EFFORTS)
        warning = EFFORT_WARNING
    elif model.thinking is not False:
        published = (efforts or {}).get(model_id, ())
        if not published:
            base_id = re.sub(
                r"-(?:preview|exp)(?:-\d+(?:-\d+)*)?$|-\d{3}$|-latest$", "", model_id
            )
            published = (efforts or {}).get(base_id, ())
        levels = ("default", *published)
    display_name = (model.display_name or "").strip()
    if (
        not display_name
        or display_name == model_id
        or display_name.startswith("models/")
    ):
        display_name = model_id.replace("-", " ").title()
    return ModelOption(model_id, display_name, levels, warning)


class ModelCatalog(QObject):
    """Discover compatible Gemini models in a daemon thread and emit Qt signals."""

    loaded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, client: genai.Client) -> None:
        super().__init__()
        self.client = client
        self.worker_thread: threading.Thread | None = None

    def refresh(self) -> None:
        """Start discovery unless a catalog request is already running."""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return
        self.worker_thread = threading.Thread(target=self._load, daemon=True)
        self.worker_thread.start()

    def _load(self) -> None:
        """Fetch all pages and deliver the complete catalog without touching UI."""
        try:
            efforts = load_efforts()
            models: dict[str, ModelOption] = {}
            for model in self.client.models.list(
                config=types.ListModelsConfig(
                    page_size=100, http_options=types.HttpOptions(timeout=15000)
                )
            ):
                option = compatible_model(model, efforts)
                if option is not None:
                    models[option.model_id] = option
            if not models:
                raise RuntimeError(
                    "No compatible image-input, text-output Gemini chat models are available."
                )
            self.loaded.emit(
                tuple(sorted(models.values(), key=lambda option: option.display_name))
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"Could not load Gemini models: {exc}")
