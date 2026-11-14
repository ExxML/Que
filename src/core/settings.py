import base64
import json
import os
from pathlib import Path
from typing import Any

import win32crypt  # type: ignore[import-untyped]


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
