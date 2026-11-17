"""Verify model discovery against the real SDK with an offline HTTP transport."""

import threading
import unittest
from typing import Any
from unittest.mock import patch

import httpx
from google import genai
from google.genai import types
from PyQt6.QtWidgets import QApplication, QWidget

from core.model_catalog import ModelCatalog, compatible_model
from ui.input.input_settings import InputSettings


class ModelCatalogTests(unittest.TestCase):
    """Validate compatibility filtering, paginated discovery, and retry UI."""

    def setUp(self) -> None:
        """Prepare deterministic model pages and a Qt event loop."""
        self.app = QApplication.instance() or QApplication([])
        effort_patch = patch("core.model_catalog.load_efforts", return_value={})
        effort_patch.start()
        self.addCleanup(effort_patch.stop)
        self.requests: list[httpx.Request] = []
        self.fail_requests = False
        self.client = genai.Client(
            api_key="test",
            http_options=types.HttpOptions(
                httpx_client=httpx.Client(transport=httpx.MockTransport(self._respond))
            ),
        )
        self.addCleanup(self.client.close)

    def test_compatible_families_and_aliases(self) -> None:
        """Accept available multimodal chat variants without fixed model IDs."""
        for name in (
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-4-carbon",
            "gemini-2.5-flash-lite-preview-09-2025",
        ):
            with self.subTest(name=name):
                result = compatible_model(
                    types.Model(
                        name=f"models/{name}", supported_actions=["generateContent"]
                    )
                )
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.model_id, name)
                self.assertNotIn("-", result.display_name)
        result = compatible_model(
            types.Model(
                name="models/gemini-3.5-flash",
                display_name="Gemini 3.5 Flash",
                supported_actions=["generateContent"],
            )
        )
        assert result is not None
        self.assertEqual(result.display_name, "Gemini 3.5 Flash")

    def test_specialized_and_unknown_models_are_excluded(self) -> None:
        """Exclude text-only, audio, image/video generation, Live, and unknown models."""
        for name in (
            "gemma-3-27b-it",
            "gemini-1.0-pro",
            "gemini-2.0-flash",
            "gemini-embedding-001",
            "gemini-3.8-flash-tts",
            "gemini-3.8-live",
            "gemini-2.5-flash-native-audio",
            "gemini-3-pro-image",
            "imagen-4.0-generate-001",
            "veo-3.0-generate-001",
            "gemini-3.5-flash-unknown",
            "unknown-model",
        ):
            with self.subTest(name=name):
                self.assertIsNone(
                    compatible_model(
                        types.Model(name=name, supported_actions=["generateContent"])
                    )
                )
        self.assertIsNone(
            compatible_model(
                types.Model(name="gemini-3.5-flash", supported_actions=["embedContent"])
            )
        )
        self.assertIsNone(compatible_model(types.Model(name="gemini-3.5-flash")))

    def test_paginated_background_discovery(self) -> None:
        """Retrieve every page in a daemon and deliver results on the Qt thread."""
        catalog = ModelCatalog(self.client)
        results: list[Any] = []
        threads: list[threading.Thread] = []

        def record(models: object) -> None:
            """Record delivered models and the thread that delivered them."""
            results.append(models)
            threads.append(threading.current_thread())

        catalog.loaded.connect(record)
        catalog.refresh()
        assert catalog.worker_thread is not None
        self.assertTrue(catalog.worker_thread.daemon)
        catalog.worker_thread.join(timeout=5)
        self.assertFalse(catalog.worker_thread.is_alive())
        self.assertEqual(results, [])
        self.app.processEvents()
        self.assertEqual(threads, [threading.main_thread()])
        self.assertEqual(
            {model.model_id for model in results[0]},
            {"gemini-3.5-flash", "gemini-4-carbon"},
        )
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1].url.params["pageToken"], "next")

    def test_failure_and_retry(self) -> None:
        """Expose loading failures and recover without a hard-coded fallback."""
        catalog = ModelCatalog(self.client)
        errors: list[str] = []
        results: list[Any] = []
        catalog.failed.connect(errors.append)
        catalog.loaded.connect(results.append)
        self.fail_requests = True
        catalog.refresh()
        assert catalog.worker_thread is not None
        catalog.worker_thread.join(timeout=5)
        self.app.processEvents()
        self.assertEqual(results, [])
        self.assertIn("Could not load Gemini models", errors[0])
        self.fail_requests = False
        catalog.refresh()
        catalog.worker_thread.join(timeout=5)
        self.app.processEvents()
        self.assertEqual(len(results), 1)
        window = QWidget()
        settings = InputSettings(window)
        self.assertFalse(settings.model_dropdown.isEnabled())
        settings.show_model_error(errors[0])
        self.assertTrue(settings.model_dropdown.isEnabled())
        self.assertEqual(settings.models, {})
        self.assertEqual(settings.model_dropdown.current_display, "Retry models")
        settings.set_models(results[0])
        self.assertEqual(len(settings.models), 2)
        window.close()

    def _respond(self, request: httpx.Request) -> httpx.Response:
        """Serve model pages using the documented REST response schema.

        Args:
            request (httpx.Request): The SDK's outgoing request.

        Returns:
            httpx.Response: A model page or simulated authentication failure.
        """
        self.requests.append(request)
        if self.fail_requests:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "Denied",
                        "status": "PERMISSION_DENIED",
                    }
                },
            )
        if "pageToken" in request.url.params:
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-4-carbon",
                            "displayName": "Gemini 4 Carbon",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-3.5-flash",
                        "displayName": "Gemini 3.5 Flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3.8-flash-tts",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ],
                "nextPageToken": "next",
            },
        )


if __name__ == "__main__":
    unittest.main()
