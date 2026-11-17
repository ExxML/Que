import base64
import json
import os
from pathlib import Path
from typing import Any

import win32crypt  # type: ignore[import-untyped]

WHISPER_MODELS: tuple[str, ...] = ("tiny.en", "base.en", "small.en", "medium.en")
CONTEXT_DOCUMENTS: dict[str, str] = {
    "resume": "Resume",
    "cover_letter": "Cover letter",
    "interview_script": "Interview script",
}


class SettingsStore:
    """Persist provider defaults and protect the Gemini key with Windows DPAPI."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            path or Path(__file__).resolve().parent.parent / "data" / "config.json"
        )
        self.data: dict[str, Any] = {}
        self.error = ""
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Settings must contain a JSON object.")
                self.data = data
        except (OSError, ValueError):
            self.error = "Could not read saved settings. Choose your settings again to repair them automatically."

    def provider(self) -> str:
        """Return the saved provider, defaulting to Gemini.

        Returns:
            str: The selected provider identifier.
        """
        return "claude" if self.data.get("provider") == "claude" else "gemini"

    def defaults(self, provider: str) -> tuple[str, str]:
        """Read independent model and effort defaults for a provider.

        Args:
            provider (str): The provider identifier.

        Returns:
            tuple[str, str]: The model identifier and effort level.
        """
        values = self.data.get(provider, {})
        if not isinstance(values, dict):
            return "", "default"
        return str(values.get("model", "")), str(values.get("effort", "default"))

    def claude_directory(self) -> str:
        """Return the saved default Claude working directory.

        Returns:
            str: The directory path, or an empty string when unset.
        """
        return str(self.data.get("claude_directory", ""))

    def whisper_model(self) -> str:
        """Return the saved transcription model, defaulting to small.en.

        Returns:
            str: The faster-whisper model name.
        """
        model = self.data.get("whisper_model")
        return str(model) if model in WHISPER_MODELS else "small.en"

    def system_tray(self) -> bool:
        """Return whether to show the system tray icon, defaulting to shown.

        Returns:
            bool: True when the tray icon should be visible.
        """
        return self.data.get("system_tray") is not False

    def capture_hidden(self) -> bool:
        """Return whether to hide the window from screen capture, defaulting to hidden.

        Returns:
            bool: True when screenshots and recordings should omit the window.
        """
        return self.data.get("capture_hidden") is not False

    def click_through(self) -> bool:
        """Return whether mouse input passes through the window, defaulting to off.

        Returns:
            bool: True when the window should ignore mouse input.
        """
        return self.data.get("click_through") is True

    def elevate_helper(self) -> bool:
        """Return whether to start the input helper as administrator, defaulting to off.

        Returns:
            bool: True when Que should ask for administrator permission at startup.
        """
        return self.data.get("elevate_helper") is True

    def context_documents(self) -> dict[str, tuple[str, str]]:
        """Return the saved documents that give the AI context about the user.

        Returns:
            dict[str, tuple[str, str]]: The file name and extracted text by document kind.
        """
        documents = self.data.get("context_documents")
        if not isinstance(documents, dict):
            return {}
        return {
            kind: (str(document.get("name", "")), str(document["text"]))
            for kind, document in documents.items()
            if kind in CONTEXT_DOCUMENTS
            and isinstance(document, dict)
            and document.get("text")
        }

    def gemini_key(self) -> str:
        """Decrypt the key for the current Windows user.

        Returns:
            str: The saved API key, or an empty string when unavailable.
        """
        encrypted = self.data.get("gemini_key", "")
        if not encrypted:
            return ""
        try:
            return bytes(
                win32crypt.CryptUnprotectData(
                    base64.b64decode(encrypted), None, None, None, 0
                )[1]
            ).decode("utf-8")
        except Exception:  # noqa: BLE001
            self.error = "The saved Gemini key cannot be opened by this Windows account. Paste it again."
            return ""

    def save(self, provider: str, model: str, effort: str, api_key: str) -> None:
        """Atomically save defaults and an encrypted Gemini credential.

        Args:
            provider (str): The provider to use.
            model (str): Its default model.
            effort (str): Its default effort.
            api_key (str): The Gemini API key, or an empty string to remove it.
        """
        data = dict(self.data)
        data["provider"] = provider
        data[provider] = {"model": model, "effort": effort}
        data["gemini_key"] = (
            base64.b64encode(
                win32crypt.CryptProtectData(
                    api_key.encode("utf-8"), "Que Gemini API key", None, None, None, 0
                )
            ).decode("ascii")
            if api_key
            else ""
        )
        self._write(data)
        self.error = ""

    def save_claude_directory(self, directory: str) -> None:
        """Atomically save the default Claude working directory.

        Args:
            directory (str): The directory new sessions start in.
        """
        self._write({**self.data, "claude_directory": directory})

    def save_whisper_model(self, model: str) -> None:
        """Atomically save the transcription model.

        Args:
            model (str): The faster-whisper model name.
        """
        self._write({**self.data, "whisper_model": model})

    def save_system_tray(self, enabled: bool) -> None:
        """Atomically save whether to show the system tray icon.

        Args:
            enabled (bool): Whether the tray icon should be visible.
        """
        self._write({**self.data, "system_tray": enabled})

    def save_capture_hidden(self, hidden: bool) -> None:
        """Atomically save whether to hide the window from screen capture.

        Args:
            hidden (bool): Whether screenshots and recordings should omit the window.
        """
        self._write({**self.data, "capture_hidden": hidden})

    def save_click_through(self, enabled: bool) -> None:
        """Atomically save whether mouse input passes through the window.

        Args:
            enabled (bool): Whether the window should ignore mouse input.
        """
        self._write({**self.data, "click_through": enabled})

    def save_elevate_helper(self, enabled: bool) -> None:
        """Atomically save whether to start the input helper as administrator.

        Args:
            enabled (bool): Whether Que should ask for administrator permission at startup.
        """
        self._write({**self.data, "elevate_helper": enabled})

    def save_context_document(self, kind: str, name: str, text: str) -> None:
        """Atomically save a context document, or remove it when the text is empty.

        Args:
            kind (str): The document kind from CONTEXT_DOCUMENTS.
            name (str): The original file name to display.
            text (str): The extracted document text.
        """
        documents = {
            saved_kind: {"name": saved_name, "text": saved_text}
            for saved_kind, (saved_name, saved_text) in self.context_documents().items()
            if saved_kind != kind
        }
        if text:
            documents[kind] = {"name": name, "text": text}
        self._write({**self.data, "context_documents": documents})

    def _write(self, data: dict[str, Any]) -> None:
        """Atomically replace the settings file and adopt its contents.

        Args:
            data (dict[str, Any]): The complete settings to save.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        self.data = data
