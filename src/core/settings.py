import base64
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import win32crypt  # type: ignore[import-untyped]

from core.hook_engine import PRESET_COUNT

WHISPER_MODELS: tuple[str, ...] = ("parakeet-tdt-0.6b-v2",)  # Transcription models
CONTEXT_DOCUMENTS: dict[str, str] = {
    "resume": "Resume",
    "cover_letter": "Cover letter",
    "interview_script": "Interview script",
}
CONFIG_GROUPS: dict[str, dict[str, str]] = {
    "AI": {
        "provider": "AI provider",
        "gemini": "Gemini default model and effort",
        "claude": "Claude default model and effort",
        "gemini_key": "Gemini API key",
        "claude_directory": "Default Claude directory",
        **{
            f"presets.{provider}.{slot}": f"{provider.title()} preset · Caps Lock+{(slot + 1) % 10}"
            for provider in ("gemini", "claude")
            for slot in range(PRESET_COUNT)
        },
    },
    "Context": {
        "instruction": "Instruction",
        "job_details": "Job and interview details",
        **{
            f"context_documents.{kind}": name
            for kind, name in CONTEXT_DOCUMENTS.items()
        },
        "resume_details": "Resume detail links",
    },
    "Voice": {"whisper_model": "Transcription model"},
    "Audio": {
        "audio_device_you": "Microphone",
        "audio_device_them": "Playback device",
    },
    "Window": {
        "system_tray": "Show system tray icon",
        "capture_hidden": "Hide from screen recording",
        "click_through": "Click through window",
        "elevate_helper": "Allow shortcuts in admin apps",
    },
}
CONFIG_KEYS = frozenset(key for group in CONFIG_GROUPS.values() for key in group)


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

    def presets(self, provider: str) -> list[tuple[str, str]]:
        """Return a provider's model presets, one per number key shortcut.

        Args:
            provider (str): The provider identifier.

        Returns:
            list[tuple[str, str]]: The model identifier and effort level of each preset, with an empty model when unset.
        """
        saved = self.data.get("presets", {})
        saved = saved.get(provider, []) if isinstance(saved, dict) else []
        saved = saved if isinstance(saved, list) else []
        presets = [
            (str(preset.get("model", "")), str(preset.get("effort", "default")))
            if isinstance(preset, dict)
            else ("", "default")
            for preset in saved[:PRESET_COUNT]
        ]
        return presets + [("", "default")] * (PRESET_COUNT - len(presets))

    def claude_directory(self) -> str:
        """Return the saved default Claude working directory.

        Returns:
            str: The directory path, or an empty string when unset.
        """
        return str(self.data.get("claude_directory", ""))

    def whisper_model(self) -> str:
        """Return the saved transcription model, defaulting to the first one.

        Returns:
            str: The transcription model name.
        """
        model = self.data.get("whisper_model")
        return str(model) if model in WHISPER_MODELS else WHISPER_MODELS[0]

    def audio_device(self, source: str) -> str:
        """Return the saved WASAPI device name or an empty string for the Windows default.

        Args:
            source (str): The transcript source, you or them.

        Returns:
            str: The device name to match when recording starts.
        """
        name = self.data.get(f"audio_device_{source}")
        return name if isinstance(name, str) else ""

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

    def instruction(self) -> str:
        """Return the saved instruction that starts every new conversation.

        Returns:
            str: The instruction, or an empty string when unset.
        """
        return str(self.data.get("instruction", ""))

    def job_details(self) -> str:
        """Return the saved job description and interview details that start every new conversation.

        Returns:
            str: The details, or an empty string when unset.
        """
        return str(self.data.get("job_details", ""))

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

    def resume_details(self) -> list[tuple[str, str]]:
        """Return the saved links from resume items to the files or folders that explain them.

        Returns:
            list[tuple[str, str]]: The item name and file or folder path of each link, in order.
        """
        details = self.data.get("resume_details")
        if not isinstance(details, list):
            return []
        return [
            (str(detail.get("name", "")), str(detail["path"]))
            for detail in details
            if isinstance(detail, dict) and detail.get("path")
        ]

    def gemini_key(self) -> str:
        """Decrypt the key for the current Windows user.

        Returns:
            str: The saved API key, or an empty string when unavailable.
        """
        encrypted = self.data.get("gemini_key", "")
        if not encrypted:
            return ""
        try:
            return _decrypt_key(encrypted)
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

    def save_presets(self, provider: str, presets: list[tuple[str, str]]) -> None:
        """Atomically save a provider's model presets.

        Args:
            provider (str): The provider identifier.
            presets (list[tuple[str, str]]): The model identifier and effort level of each preset.
        """
        saved = self.data.get("presets")
        self._write(
            {
                **self.data,
                "presets": {
                    **(saved if isinstance(saved, dict) else {}),
                    provider: [
                        {"model": model, "effort": effort} for model, effort in presets
                    ],
                },
            }
        )

    def save_claude_directory(self, directory: str) -> None:
        """Atomically save the default Claude working directory.

        Args:
            directory (str): The directory new sessions start in.
        """
        self._write({**self.data, "claude_directory": directory})

    def save_whisper_model(self, model: str) -> None:
        """Atomically save the transcription model.

        Args:
            model (str): The transcription model name.
        """
        self._write({**self.data, "whisper_model": model})

    def save_audio_device(self, source: str, name: str) -> None:
        """Atomically save one transcript source's audio device.

        Args:
            source (str): The transcript source, you or them.
            name (str): The WASAPI device name, or an empty string for the Windows default.
        """
        self._write({**self.data, f"audio_device_{source}": name})

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

    def save_instruction(self, instruction: str) -> None:
        """Atomically save the instruction that starts every new conversation.

        Args:
            instruction (str): The instruction, or an empty string to remove it.
        """
        self._write({**self.data, "instruction": instruction})

    def save_job_details(self, job_details: str) -> None:
        """Atomically save the job description and interview details that start every new conversation.

        Args:
            job_details (str): The details, or an empty string to remove them.
        """
        self._write({**self.data, "job_details": job_details})

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

    def save_resume_details(self, details: list[tuple[str, str]]) -> None:
        """Atomically replace the links from resume items to their files or folders.

        Args:
            details (list[tuple[str, str]]): The item name and file or folder path of each link.
        """
        self._write(
            {
                **self.data,
                "resume_details": [
                    {"name": name, "path": path} for name, path in details
                ],
            }
        )

    def config_values(self) -> dict[str, Any]:
        """Collect exportable preferences, including defaults and the encrypted credential.

        Returns:
            dict[str, Any]: Saved settings keyed by their import selection identifiers.
        """
        documents = self.context_documents()
        encrypted_key = (
            str(self.data.get("gemini_key", "")) if self.gemini_key() else ""
        )
        return {
            "provider": self.provider(),
            **{
                provider: _model_settings(*self.defaults(provider))
                for provider in ("gemini", "claude")
            },
            "gemini_key": encrypted_key,
            "claude_directory": self.claude_directory(),
            **{
                f"presets.{provider}.{slot}": _model_settings(*preset)
                for provider in ("gemini", "claude")
                for slot, preset in enumerate(self.presets(provider))
            },
            "instruction": self.instruction(),
            "job_details": self.job_details(),
            **{
                f"context_documents.{kind}": dict(
                    zip(("name", "text"), documents[kind], strict=True)
                )
                if kind in documents
                else None
                for kind in CONTEXT_DOCUMENTS
            },
            "resume_details": [
                {"name": name, "path": path} for name, path in self.resume_details()
            ],
            "whisper_model": self.whisper_model(),
            "audio_device_you": self.audio_device("you"),
            "audio_device_them": self.audio_device("them"),
            "system_tray": self.system_tray(),
            "capture_hidden": self.capture_hidden(),
            "click_through": self.click_through(),
            "elevate_helper": self.elevate_helper(),
        }

    def export_config(self, path: Path) -> None:
        """Write a versioned settings backup atomically outside Que's data folder.

        Args:
            path (Path): The destination chosen by the user.
        """
        if path.resolve().is_relative_to(self.path.resolve().parent):
            raise ValueError("Choose a destination outside Que’s data folder.")
        backup = {
            "format": "que-settings",
            "version": 1,
            "settings": self.config_values(),
        }
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete_on_close=False
        ) as temporary:
            json.dump(backup, temporary, ensure_ascii=False, indent=2)
            temporary.close()
            os.replace(temporary.name, path)

    def read_config(self, path: Path) -> tuple[dict[str, Any], bool]:
        """Validate a backup and return only settings available for selection.

        Args:
            path (Path): The backup chosen by the user.

        Returns:
            tuple[dict[str, Any], bool]: Selectable values and whether the key is unavailable.
        """
        backup = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(backup, dict) or backup.get("format") != "que-settings":
            raise ValueError("Choose a JSON settings backup exported by Que.")
        if type(backup.get("version")) is not int or backup["version"] != 1:
            raise ValueError("This backup uses an unsupported settings format.")
        values = backup.get("settings")
        if not isinstance(values, dict) or not values.keys() & CONFIG_KEYS:
            raise ValueError("This backup contains no recognized settings.")
        settings = {key: value for key, value in values.items() if key in CONFIG_KEYS}
        for key, value in settings.items():
            if not _valid_config_value(key, value):
                label = next(
                    group[key] for group in CONFIG_GROUPS.values() if key in group
                )
                raise ValueError(f"The backup contains an invalid value for {label}.")
        encrypted = settings.get("gemini_key")
        if encrypted:
            try:
                _decrypt_key(encrypted)
            except Exception:  # noqa: BLE001
                del settings["gemini_key"]
                return settings, True
        return settings, False

    def import_config(self, settings: dict[str, Any]) -> None:
        """Merge reviewed preferences into the live config with one atomic write.

        Args:
            settings (dict[str, Any]): The selected values from a validated backup.
        """
        data = dict(self.data)
        for key in settings.keys() & CONFIG_KEYS:
            value = settings[key]
            parent, separator, child = key.partition(".")
            if not separator:
                data[parent] = value
                continue
            existing = data.get(parent)
            group = dict(existing) if isinstance(existing, dict) else {}
            if parent == "presets":
                provider, _, slot = child.partition(".")
                rows = group.get(provider)
                presets = list(rows) if isinstance(rows, list) else []
                presets.extend(
                    _model_settings("", "default")
                    for _ in range(PRESET_COUNT - len(presets))
                )
                presets[int(slot)] = value
                group[provider] = presets
            elif value is None:
                group.pop(child, None)
            else:
                group[child] = value
            data[parent] = group
        self._write(data)
        self.error = ""

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


def _decrypt_key(encrypted: str) -> str:
    """Decrypt a Gemini credential using the current Windows account."""
    return bytes(
        win32crypt.CryptUnprotectData(base64.b64decode(encrypted), None, None, None, 0)[
            1
        ]
    ).decode("utf-8")


def _model_settings(model: str, effort: str) -> dict[str, str]:
    """Represent a model and its compatible effort as one setting."""
    return {"model": model, "effort": effort}


def _has_string_fields(value: Any, *fields: str) -> bool:
    """Check an imported object against an exact set of string-valued fields."""
    return (
        isinstance(value, dict)
        and value.keys() == set(fields)
        and all(isinstance(value[field], str) for field in fields)
    )


def _valid_config_value(key: str, value: Any) -> bool:
    """Check the type and shape of a setting before presenting it for import."""
    if key == "provider":
        return value in ("gemini", "claude")
    if key == "whisper_model":
        return value in WHISPER_MODELS
    if key in CONFIG_GROUPS["Window"]:
        return isinstance(value, bool)
    if key in ("gemini", "claude") or key.startswith("presets."):
        return _has_string_fields(value, "model", "effort")
    if key.startswith("context_documents."):
        return value is None or (
            _has_string_fields(value, "name", "text") and bool(value["text"])
        )
    if key == "resume_details":
        return isinstance(value, list) and all(
            _has_string_fields(detail, "name", "path") and bool(detail["path"])
            for detail in value
        )
    return isinstance(value, str)
