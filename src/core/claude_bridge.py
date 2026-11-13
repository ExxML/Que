import base64
import json
import mimetypes
import os
import queue
import shutil
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Generator
from uuid import uuid4

from core.model_catalog import ModelOption


class ClaudeBridge:
    """Stream the installed Claude Code CLI using its signed-in subscription."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (
            directory or Path(__file__).resolve().parent.parent / "data" / "claude"
        )
        self.lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.processes: set[subprocess.Popen[str]] = set()
        self.closed = threading.Event()

    def close(self) -> None:
        """Terminate owned CLI processes when Que exits without waiting on the UI thread."""
        self.closed.set()
        with self.process_lock:
            for process in self.processes:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass

    def discover(self) -> tuple[tuple[ModelOption, ...], str]:
        """Check subscription authentication and retrieve live model capabilities.

        Returns:
            tuple[tuple[ModelOption, ...], str]: Available models and connection status.
        """
        auth = subprocess.run(
            [*self._command(), "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=self._environment(),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            status = json.loads(auth.stdout)
            if not isinstance(status, dict):
                raise ValueError("Invalid authentication response.")
        except ValueError as exc:
            raise RuntimeError(
                "Could not check Claude Code. Update it and run claude auth login in Terminal."
            ) from exc
        if auth.returncode or not status.get("loggedIn"):
            raise RuntimeError(
                "Claude Code is not signed in. Run claude auth login in Terminal with your Pro account."
            )
        if status.get("authMethod") not in ("claude.ai", "oauth_token"):
            raise RuntimeError(
                "Claude Code must use a Claude subscription. Sign in with claude auth login in Terminal."
            )
        request_id = str(uuid4())
        payload = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "initialize"},
        }
        with closing(
            self._events(["--no-session-persistence"], payload, threading.Event(), 45)
        ) as events:
            for event in events:
                if event.get("type") != "control_response":
                    continue
                response = event.get("response", {})
                if response.get("request_id") != request_id:
                    continue
                if response.get("subtype") != "success":
                    raise RuntimeError(
                        "Claude Code initialization failed. Update Claude Code and retry."
                    )
                models = self.parse_models(
                    response.get("response", {}).get("models", [])
                )
                if not models:
                    raise RuntimeError(
                        "Claude Code returned no models. Update Claude Code and check the connection again."
                    )
                plan = str(status.get("subscriptionType") or "subscription").title()
                return (
                    models,
                    f"Connected to Claude Code · {plan}. Uses your subscription limits.",
                )
        raise RuntimeError(
            "Claude Code did not return its model catalog. Update Claude Code and retry."
        )

    @staticmethod
    def parse_models(rows: list[dict[str, Any]]) -> tuple[ModelOption, ...]:
        """Read model and effort options from the CLI initialization response.

        Args:
            rows (list[dict[str, Any]]): CLI model metadata.

        Returns:
            tuple[ModelOption, ...]: Unique choices with only advertised effort levels.
        """
        models: dict[str, ModelOption] = {}
        for row in rows:
            value = row.get("value")
            if not isinstance(value, str) or not value:
                continue
            advertised = row.get("supportedEffortLevels") or []
            if not isinstance(advertised, list):
                advertised = []
            levels = tuple(
                dict.fromkeys(
                    level for level in advertised if isinstance(level, str) and level
                )
            )
            if row.get("supportsEffort") is False:
                levels = ()
            models[value] = ModelOption(
                value, str(row.get("displayName") or value), ("default", *levels)
            )
        return tuple(models.values())

    def send_message(
        self,
        message: str,
        attachments: tuple[str, ...],
        model: str,
        effort: str,
        session_id: str,
        checkpoint: str | None,
        on_chunk: Callable[[str], None],
        stop_flag: threading.Event,
    ) -> str | None:
        """Resume one Que chat at its last accepted assistant message.

        Args:
            message (str): The new user message.
            attachments (tuple[str, ...]): Screenshot or document paths.
            model (str): The CLI model identifier.
            effort (str): The advertised effort or default.
            session_id (str): The conversation UUID owned by this Que chat.
            checkpoint (str | None): The last accepted assistant UUID.
            on_chunk (Callable[[str], None]): Deliver visible text.
            stop_flag (threading.Event): Cancel a running or queued turn.

        Returns:
            str | None: A completed assistant UUID, or None after cancellation.
        """
        while not self.lock.acquire(timeout=0.1):
            if stop_flag.is_set():
                return None
        try:
            if stop_flag.is_set():
                return None
            content = self._content(message, attachments)
            arguments = [f"--model={model}"]
            if effort != "default":
                arguments.append(f"--effort={effort}")
            if checkpoint:
                arguments.extend(
                    [f"--resume={session_id}", f"--resume-session-at={checkpoint}"]
                )
            else:
                arguments.append(f"--session-id={session_id}")
            payload = {
                "type": "user",
                "session_id": session_id,
                "client_composed": True,
                "message": {"role": "user", "content": content},
            }
            assistant_id = ""
            streamed = False
            with closing(self._events(arguments, payload, stop_flag, 600)) as events:
                for event in events:
                    kind = event.get("type")
                    if event.get("parent_tool_use_id"):
                        continue
                    if kind == "stream_event":
                        delta = event.get("event", {}).get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            streamed = True
                            on_chunk(str(delta["text"]))
                    elif kind == "assistant":
                        assistant_id = str(event.get("uuid") or "")
                        if not streamed:
                            for block in event.get("message", {}).get("content", []):
                                if block.get("type") == "text":
                                    on_chunk(str(block.get("text", "")))
                    elif kind == "result":
                        if event.get("is_error") or event.get("subtype") != "success":
                            detail = (
                                event.get("result")
                                or event.get("errors")
                                or "Claude request failed."
                            )
                            raise RuntimeError(str(detail))
                        if event.get("session_id") != session_id or not assistant_id:
                            raise RuntimeError(
                                "Claude returned an incomplete conversation checkpoint."
                            )
                        return None if stop_flag.is_set() else assistant_id
            if stop_flag.is_set():
                return None
            raise RuntimeError(
                "Claude ended before completing the response. Retry, or start a new chat."
            )
        finally:
            self.lock.release()

    def _command(self) -> list[str]:
        """Find the native CLI or the JavaScript entry point of an npm install.

        Returns:
            list[str]: The native executable or Node and the npm CLI entry point.
        """
        executable = shutil.which("claude.exe") or shutil.which("claude")
        if executable and Path(executable).suffix.lower() == ".exe":
            return [executable]
        native = Path.home() / ".local" / "bin" / "claude.exe"
        if native.is_file():
            return [str(native)]
        if executable:
            entry = (
                Path(executable).parent
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "cli.js"
            )
            node = shutil.which("node.exe")
            if node and entry.is_file():
                return [node, str(entry)]
        raise RuntimeError(
            "Claude Code was not found. Install Claude Code Terminal for Windows, then restart Que."
        )

    def _environment(self) -> dict[str, str]:
        """Keep subscription login and exclude inherited API billing overrides.

        Returns:
            dict[str, str]: The child process environment.
        """
        environment = dict(os.environ)
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        ):
            environment.pop(key, None)
        return environment

    def _content(
        self, message: str, attachments: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Encode attachments directly so Claude needs no filesystem tools.

        Args:
            message (str): The user's text.
            attachments (tuple[str, ...]): Files to attach.

        Returns:
            list[dict[str, Any]]: Claude content blocks.
        """
        content: list[dict[str, Any]] = []
        for filename in attachments:
            path = Path(filename)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            if mime in (
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "application/pdf",
            ):
                content.append(
                    {
                        "type": "document" if mime == "application/pdf" else "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                        },
                    }
                )
            elif mime.startswith("text/"):
                content.append(
                    {
                        "type": "text",
                        "text": f"{path.name}:\n{path.read_text(encoding='utf-8')}",
                    }
                )
            else:
                raise ValueError(
                    f"Claude cannot accept {path.name}. Use an image, PDF, or text file, or select Gemini."
                )
        content.append({"type": "text", "text": message})
        return content

    def _events(
        self,
        arguments: list[str],
        payload: dict[str, Any],
        stop_flag: threading.Event,
        timeout: float,
    ) -> Generator[dict[str, Any], None, None]:
        """Drain CLI pipes while supporting cancellation even before the first token.

        Args:
            arguments (list[str]): Per-request CLI flags.
            payload (dict[str, Any]): The JSON input message.
            stop_flag (threading.Event): The request cancellation flag.
            timeout (float): Maximum request duration in seconds.

        Yields:
            dict[str, Any]: Parsed protocol messages.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        command = [
            *self._command(),
            "--print",
            "--verbose",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--include-partial-messages",
            "--tools=",
            "--strict-mcp-config",
            "--setting-sources=",
            "--disable-slash-commands",
            "--settings",
            '{"disableAllHooks":true}',
            "--no-chrome",
            *arguments,
        ]
        with self.process_lock:
            if self.closed.is_set() or stop_flag.is_set():
                return
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.directory,
                env=self._environment(),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.processes.add(process)
        messages: queue.Queue[str | None] = queue.Queue()
        errors: list[str] = []
        reader = threading.Thread(
            target=self._read_stdout, args=(process, messages), daemon=True
        )
        error_reader = threading.Thread(
            target=self._read_stderr, args=(process, errors), daemon=True
        )
        reader.start()
        error_reader.start()
        deadline = time.monotonic() + timeout
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            while not stop_flag.is_set():
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Claude Code timed out. Check your connection and retry."
                    )
                try:
                    line = messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    if process.wait(timeout=5) != 0:
                        raise RuntimeError(
                            "Claude Code failed. " + "".join(errors)[-1500:]
                        )
                    return
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise RuntimeError(
                        "Claude Code returned invalid stream data. Update it and retry."
                    ) from exc
                if isinstance(event, dict):
                    yield event
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                if stop_flag.is_set() and process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            error_reader.join(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            with self.process_lock:
                self.processes.discard(process)

    @staticmethod
    def _read_stdout(
        process: subprocess.Popen[str], messages: queue.Queue[str | None]
    ) -> None:
        """Read protocol lines into a queue and signal end of stream.

        Args:
            process (subprocess.Popen[str]): The running CLI.
            messages (queue.Queue[str | None]): The worker's incoming messages.
        """
        assert process.stdout is not None
        try:
            for line in process.stdout:
                if line.strip():
                    messages.put(line)
        finally:
            messages.put(None)

    @staticmethod
    def _read_stderr(process: subprocess.Popen[str], errors: list[str]) -> None:
        """Drain diagnostics without filling the subprocess pipe.

        Args:
            process (subprocess.Popen[str]): The running CLI.
            errors (list[str]): The bounded diagnostic buffer.
        """
        assert process.stderr is not None
        for line in process.stderr:
            errors.append(line)
            del errors[:-20]
