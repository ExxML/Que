"""Verify the Interactions wire format and conversation lifecycle without network access."""

import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
from google import genai
from google.genai import types
from qt_application import application

from core.ai_receiver import AIReceiver
from core.ai_sender import AISender
from core.model_catalog import ModelOption

TEST_MODELS = (
    ModelOption(
        "gemini-3-flash-preview",
        "Gemini 3 Flash Preview",
        ("minimal", "low", "medium", "high"),
    ),
    ModelOption("gemini-2.5-flash", "Gemini 2.5 Flash", ("low", "medium", "high")),
)


class InteractionsTests(unittest.TestCase):
    """Exercise real SDK serialization and parsing with a fake HTTP transport."""

    def setUp(self) -> None:
        """Create a sender with an isolated mocked transport."""
        self.app = application()
        self.bodies: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = [
            {
                "event_type": "interaction.created",
                "interaction": {"id": "new", "status": "in_progress"},
            },
            {
                "event_type": "step.delta",
                "index": 0,
                "delta": {"type": "text", "text": "Hello"},
            },
            {
                "event_type": "interaction.completed",
                "interaction": {"id": "new", "status": "completed"},
            },
        ]
        self.status = 200
        self.responses: list[httpx.Response] = []
        client = genai.Client(
            api_key="test",
            http_options=types.HttpOptions(
                httpx_client=httpx.Client(transport=httpx.MockTransport(self._respond))
            ),
        )
        settings = MagicMock(
            **{"gemini_key.return_value": "", "provider.return_value": "gemini"}
        )
        self.sender = AISender(settings, client=client)
        self.sender.set_models(TEST_MODELS)
        self.addCleanup(client.close)

    def test_wire_format_and_chaining(self) -> None:
        """Send only new inputs and commit only after completion acceptance."""
        chunks: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "screen.png"
            image.write_bytes(b"screenshot")
            request = self.sender.prepare_request("first", [str(image)])
            result = self.sender.send_message(request, chunks.append, threading.Event())
        self.assertEqual(result, "new")
        self.assertIsNone(self.sender.previous_interaction_id)
        self.assertTrue(self.sender.commit_interaction(request, result or ""))
        self.assertFalse(self.sender.commit_interaction(request, "duplicate"))
        self.sender.send_message(
            self.sender.prepare_request("second"), chunks.append, threading.Event()
        )
        self.assertEqual(
            self.bodies[0]["input"][0]["content"][0]["data"],
            base64.b64encode(b"screenshot").decode(),
        )
        self.assertEqual(
            self.bodies[1]["input"],
            [{"type": "user_input", "content": [{"type": "text", "text": "second"}]}],
        )
        self.assertEqual(self.bodies[1]["previous_interaction_id"], "new")
        self.assertTrue(self.bodies[1]["store"])
        self.assertEqual(chunks, ["Hello", "Hello"])
        self.assertTrue(all(response.is_closed for response in self.responses))

    def test_failed_and_truncated_streams_keep_context(self) -> None:
        """Reject partial streams, streaming errors, and unsuccessful completion."""
        for tail in (
            [],
            [{"event_type": "error", "error": {"message": "failed"}}],
            [
                {
                    "event_type": "interaction.completed",
                    "interaction": {"id": "bad", "status": "incomplete"},
                }
            ],
        ):
            with self.subTest(tail=tail):
                self.sender.previous_interaction_id = "saved"
                self.events = self.events[:2] + tail
                with self.assertRaisesRegex(RuntimeError, "Context has not been reset"):
                    self.sender.send_message(
                        self.sender.prepare_request("turn"),
                        lambda _: None,
                        threading.Event(),
                    )
                self.assertEqual(self.sender.previous_interaction_id, "saved")
                self.assertTrue(self.responses[-1].is_closed)

    def test_text_attachment_is_sent_as_text_content(self) -> None:
        """Serialize UTF-8 text attachments as supported text parts instead of documents."""
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "fixture.txt"
            attachment.write_text("Fixture café", encoding="utf-8")
            self.sender.send_message(
                self.sender.prepare_request("read", [str(attachment)]),
                lambda _: None,
                threading.Event(),
            )
        content = self.bodies[-1]["input"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "Fixture café"})

    def test_request_failure_does_not_guess_conversation_expiration(self) -> None:
        """Keep ordinary request errors distinct from a reported missing interaction."""
        self.sender.previous_interaction_id = "saved"
        self.status = 400
        with self.assertRaises(RuntimeError) as raised:
            self.sender.send_message(
                self.sender.prepare_request("turn"), lambda _: None, threading.Event()
            )
        self.assertNotIn(
            "saved interaction is expired or unavailable", str(raised.exception)
        )
        self.assertIn("Context has not been reset", str(raised.exception))
        self.assertEqual(self.sender.previous_interaction_id, "saved")

    def test_unavailable_context_has_no_fallback(self) -> None:
        """Surface an expired server chain without starting a fresh conversation."""
        self.sender.previous_interaction_id = "expired"
        self.status = 404
        with self.assertRaisesRegex(RuntimeError, "expired or unavailable"):
            self.sender.send_message(
                self.sender.prepare_request("turn"), lambda _: None, threading.Event()
            )
        self.assertEqual(len(self.bodies), 1)
        self.assertEqual(self.sender.previous_interaction_id, "expired")

    def test_cancellation_closes_stream(self) -> None:
        """Discard completion when cancellation happens during streamed output."""
        flag = threading.Event()
        result = self.sender.send_message(
            self.sender.prepare_request("turn"), lambda _: flag.set(), flag
        )
        self.assertIsNone(result)
        self.assertTrue(self.responses[-1].is_closed)
        self.assertIsNone(self.sender.previous_interaction_id)

    def test_reset_and_supersession_reject_old_completion(self) -> None:
        """Reject delayed results after Clear, exit, or a replacement request."""
        old = self.sender.prepare_request("old")
        self.sender.reset_chat()
        self.assertFalse(self.sender.commit_interaction(old, "old-id"))
        fresh = self.sender.prepare_request("fresh")
        self.assertFalse(self.sender.commit_interaction(old, "old-id"))
        self.assertTrue(self.sender.commit_interaction(fresh, "fresh-id"))
        self.sender.reset_chat()
        self.assertIsNone(self.sender.previous_interaction_id)

    def test_model_effort_and_attachment_snapshot(self) -> None:
        """Keep request inputs stable while subsequent settings change."""
        attachments = ["one.png"]
        request = self.sender.prepare_request("old", attachments)
        attachments.clear()
        self.sender.set_model("gemini-2.5-flash")
        self.sender.set_effort_level("high")
        self.assertEqual(request.attachments, ("one.png",))
        self.assertEqual(request.effort_level, "minimal")
        self.sender.send_message(
            self.sender.prepare_request("new"), lambda _: None, threading.Event()
        )
        self.assertEqual(self.bodies[-1]["model"], "gemini-2.5-flash")
        self.assertEqual(self.bodies[-1]["generation_config"]["thinking_level"], "high")

    def test_each_supported_effort_reaches_api(self) -> None:
        """Send every supported model and effort combination unchanged."""
        self.sender.previous_interaction_id = "saved"
        for option in TEST_MODELS:
            model, levels = option.model_id, option.effort_levels
            for level in levels:
                with self.subTest(model=model, level=level):
                    self.sender.set_model(model)
                    self.sender.set_effort_level(level)
                    self.sender.send_message(
                        self.sender.prepare_request("turn"),
                        lambda _: None,
                        threading.Event(),
                    )
                    self.assertEqual(
                        self.bodies[-1]["generation_config"], {"thinking_level": level}
                    )
                    self.assertEqual(self.sender.previous_interaction_id, "saved")
        self.sender.set_model("gemini-2.5-flash")
        with self.assertRaises(ValueError):
            self.sender.set_effort_level("minimal")

    def test_qt_delivery_and_cancel_before_queued_completion(self) -> None:
        """Keep UI and commits on the main thread and reject queued canceled results."""
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.sender.reset_chat()
                chat = MagicMock()
                chat.streaming_bubble = None
                chat.streaming_text = "Hello"
                chat.add_message.side_effect = self._assert_main_thread
                chat.append_to_stream.side_effect = self._assert_main_thread
                chat.finalize_assistant_stream.side_effect = self._assert_main_thread
                receiver = AIReceiver(self.sender, chat)
                turns: list[tuple[str, str, str]] = []
                receiver.turn_committed.connect(
                    lambda request, interaction_id, text, turns=turns: turns.append(
                        (request.message, interaction_id, text)
                    )
                )
                receiver.handle_message("hello")
                chat.add_message.assert_called_once_with(
                    "hello", is_user=True, transcript="", screenshots=()
                )
                assert receiver.ai_thread is not None
                self.assertTrue(receiver.ai_thread.daemon)
                receiver.ai_thread.join(timeout=5)
                self.assertFalse(receiver.ai_thread.is_alive())
                self.assertIsNone(self.sender.previous_interaction_id)
                if cancel:
                    receiver.stop()
                self.app.processEvents()
                self.assertEqual(
                    self.sender.previous_interaction_id, None if cancel else "new"
                )
                self.assertEqual(turns, [] if cancel else [("hello", "new", "Hello")])
                if cancel:
                    chat.append_to_stream.assert_not_called()
                else:
                    chat.append_to_stream.assert_called_once_with("Hello")

    def _assert_main_thread(self, *_args: Any, **_kwargs: Any) -> None:
        """Assert that a UI call runs on the test's Qt thread."""
        self.assertIs(threading.current_thread(), threading.main_thread())

    def _respond(self, request: httpx.Request) -> httpx.Response:
        """Return deterministic SSE data and capture outgoing request JSON.

        Args:
            request (httpx.Request): The outgoing SDK request.

        Returns:
            httpx.Response: The simulated Gemini response.
        """
        self.bodies.append(json.loads(request.content))
        data = "".join(
            f"event: {event['event_type']}\ndata: {json.dumps(event)}\n\n"
            for event in self.events
        )
        response = httpx.Response(
            self.status,
            headers={"content-type": "text/event-stream"},
            content=data
            if self.status == 200
            else '{"error":{"message":"Interaction expired","code":404}}',
        )
        self.responses.append(response)
        return response


if __name__ == "__main__":
    unittest.main()
