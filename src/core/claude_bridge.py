import base64
import json
import mimetypes
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.model_catalog import ModelOption

WARM_DELAY = 0.3  # Seconds a turn's settings must stay unchanged before its CLI starts
WARM_IDLE = 600.0  # Seconds an unused pre-started CLI waits before it is closed


@dataclass(frozen=True)
class ClaudeTurn:
    """Describe the CLI a turn needs, so one can start before the message is sent."""

    model: str
    effort: str
    session_id: str
    checkpoint: str | None
    directory: Path | None
    folders: tuple[Path, ...]


class ClaudeBridge:
    """Stream the installed Claude Code CLI using its signed-in subscription."""

    def __init__(self, directory: Path | None = None, live: bool = False) -> None:
        # Outside any repository so Claude sees no project or git context without a chosen directory
        self.directory = directory or (
            Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Que" / "claude"
        )
        # Only the running app starts the CLI on its own, for pre-started turns and fact sheets
        self.live = live
        self.lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.processes: set[subprocess.Popen[str]] = set()
        self.closed = threading.Event()
        self.warm_lock = threading.Lock()
        self.warm_turn: ClaudeTurn | None = None
        self.warm: tuple[list[str], Path, subprocess.Popen[str]] | None = None
        self.warm_timer: threading.Timer | None = None

    def close(self) -> None:
        """Terminate owned CLI processes when Que exits without waiting on the UI thread."""
        self.closed.set()
        with self.warm_lock:
            self.warm_turn = None
            self.warm = None
            if self.warm_timer is not None:
                self.warm_timer.cancel()
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
            check=False,
        )
        try:
            status = json.loads(auth.stdout)
            if not isinstance(status, dict):
                raise ValueError("Invalid authentication response.")  # noqa: TRY004
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
            self._events(
                ["--no-session-persistence"],
                payload,
                threading.Event(),
                45,
                None,
            )
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

    def prewarm(self, turn: ClaudeTurn | None) -> None:
        """Start the CLI for the next turn ahead of time so sending skips its startup.

        A changed turn replaces the waiting CLI, and None closes it.

        Args:
            turn (ClaudeTurn | None): The next turn's settings, or None when no Claude turn is expected.
        """
        if not self.live:
            return
        with self.warm_lock:
            if turn == self.warm_turn:
                return
            self.warm_turn = turn
            self._discard_warm()
            if turn is None or self.closed.is_set():
                return
            # Wait for the settings to settle so choosing a model and effort starts one CLI, not several
            self.warm_timer = threading.Timer(WARM_DELAY, self._start_warm, (turn,))
            self.warm_timer.daemon = True
            self.warm_timer.start()

    def ask(
        self,
        prompt: str,
        model: str,
        effort: str,
        directory: Path,
        stop_flag: threading.Event,
    ) -> str:
        """Answer one prompt with read-only file tools in a folder, without saving a session.

        Args:
            prompt (str): The request.
            model (str): The CLI model identifier, or an empty string for the CLI's default.
            effort (str): The advertised effort or default.
            directory (Path): The folder Claude may read.
            stop_flag (threading.Event): Cancel the request.

        Returns:
            str: The final reply text.
        """
        arguments = ["--no-session-persistence"]
        if model:
            arguments.append(f"--model={model}")
        if effort != "default":
            arguments.append(f"--effort={effort}")
        payload = {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
        with closing(
            self._events(arguments, payload, stop_flag, 900, directory, warm=False)
        ) as events:
            for event in events:
                if event.get("type") != "result":
                    continue
                if event.get("is_error") or event.get("subtype") != "success":
                    detail = (
                        event.get("result")
                        or event.get("errors")
                        or "Claude request failed."
                    )
                    raise RuntimeError(str(detail))
                return str(event.get("result") or "")
        raise RuntimeError("Claude ended before completing the response.")

    def send_message(
        self,
        message: str,
        attachments: tuple[str, ...],
        model: str,
        effort: str,
        session_id: str,
        checkpoint: str | None,
        directory: Path | None,
        on_chunk: Callable[[str], None],
        stop_flag: threading.Event,
        folders: tuple[Path, ...] = (),
        on_status: Callable[[str], None] | None = None,
    ) -> str | None:
        """Resume one Que chat at its last accepted assistant message.

        Args:
            message (str): The new user message.
            attachments (tuple[str, ...]): Screenshot or document paths.
            model (str): The CLI model identifier.
            effort (str): The advertised effort or default.
            session_id (str): The conversation UUID owned by this Que chat.
            checkpoint (str | None): The last accepted assistant UUID.
            directory (Path | None): The working directory Claude may read, or None for no file access.
            on_chunk (Callable[[str], None]): Deliver visible text.
            stop_flag (threading.Event): Cancel a running or queued turn.
            folders (tuple[Path, ...]): More folders Claude may read, such as linked resume projects.
            on_status (Callable[[str], None], optional): Show what Claude is doing while it uses a tool.

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
            arguments = self._arguments(model, effort, session_id, checkpoint)
            payload = {
                "type": "user",
                "session_id": session_id,
                "client_composed": True,
                "message": {"role": "user", "content": content},
            }
            assistant_id = ""
            streamed = False
            separate = False
            with closing(
                self._events(arguments, payload, stop_flag, 600, directory, folders)
            ) as events:
                for event in events:
                    kind = event.get("type")
                    if event.get("parent_tool_use_id"):
                        continue
                    if kind == "stream_event":
                        delta = event.get("event", {}).get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            if separate:
                                on_chunk("\n\n")
                                separate = False
                            streamed = True
                            on_chunk(str(delta["text"]))
                    elif kind == "assistant":
                        # Tool use splits a reply into several messages; keep their text apart
                        separate = streamed
                        assistant_id = str(event.get("uuid") or "")
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text" and not streamed:
                                on_chunk(str(block.get("text", "")))
                            elif block.get("type") == "tool_use" and on_status:
                                on_status(_tool_status(block))
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

    @staticmethod
    def _arguments(
        model: str, effort: str, session_id: str, checkpoint: str | None
    ) -> list[str]:
        """Build the per-turn CLI flags.

        Args:
            model (str): The CLI model identifier.
            effort (str): The advertised effort or default.
            session_id (str): The conversation UUID owned by the Que chat.
            checkpoint (str | None): The last accepted assistant UUID, or None for a new chat.

        Returns:
            list[str]: The model, effort and session flags.
        """
        arguments = [f"--model={model}"]
        if effort != "default":
            arguments.append(f"--effort={effort}")
        if checkpoint:
            arguments.extend(
                [f"--resume={session_id}", f"--resume-session-at={checkpoint}"]
            )
        else:
            arguments.append(f"--session-id={session_id}")
        return arguments

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
        directory: Path | None,
        folders: tuple[Path, ...] = (),
        warm: bool = True,
    ) -> Generator[dict[str, Any], None, None]:
        """Drain CLI pipes while supporting cancellation even before the first token.

        Args:
            arguments (list[str]): Per-request CLI flags.
            payload (dict[str, Any]): The JSON input message.
            stop_flag (threading.Event): The request cancellation flag.
            timeout (float): Maximum request duration in seconds.
            directory (Path | None): The directory that confines read-only file tools, or None to run without tools.
            folders (tuple[Path, ...]): More folders the read-only file tools may open.
            warm (bool): Whether this chat turn uses the matching pre-started CLI and closes any other.

        Yields:
            dict[str, Any]: Parsed protocol messages.
        """
        command, cwd = self._launch(arguments, directory, folders)
        process = self._take_warm(command, cwd) if warm else None
        with self.process_lock:
            if self.closed.is_set() or stop_flag.is_set():
                if process is not None:
                    self._reap(process)
                return
            if process is None:
                process = self._spawn(command, cwd)
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
            self._close_pipes(process)
            with self.process_lock:
                self.processes.discard(process)

    def _launch(
        self, arguments: list[str], directory: Path | None, folders: tuple[Path, ...]
    ) -> tuple[list[str], Path]:
        """Build the full CLI command and its working directory.

        Args:
            arguments (list[str]): Per-request CLI flags.
            directory (Path | None): The directory that confines read-only file tools, or None to run without tools.
            folders (tuple[Path, ...]): More folders the read-only file tools may open.

        Returns:
            tuple[list[str], Path]: The command line and the directory to run it in.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        if directory is not None and not directory.is_dir():
            raise RuntimeError(
                f"Claude's directory {directory} no longer exists. Click it below the chat input to choose another."
            )
        command = [
            *self._command(),
            "--print",
            "--verbose",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--include-partial-messages",
            f"--tools={'Read,Grep,Glob' if directory or folders else ''}",
            *(f"--add-dir={folder}" for folder in folders),
            "--strict-mcp-config",
            "--setting-sources=",
            "--disable-slash-commands",
            "--settings",
            '{"disableAllHooks":true}',
            "--no-chrome",
            *arguments,
        ]
        return command, directory or self.directory

    def _spawn(self, command: list[str], cwd: Path) -> subprocess.Popen[str]:
        """Start a hidden CLI process and track it for shutdown; call with the process lock held.

        Args:
            command (list[str]): The command line.
            cwd (Path): The working directory.

        Returns:
            subprocess.Popen[str]: The started process.
        """
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=self._environment(),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.processes.add(process)
        return process

    def _start_warm(self, turn: ClaudeTurn) -> None:
        """Start the CLI for a pending turn if it is still the expected one.

        Args:
            turn (ClaudeTurn): The expected next turn's settings.
        """
        try:
            command, cwd = self._launch(
                self._arguments(
                    turn.model, turn.effort, turn.session_id, turn.checkpoint
                ),
                turn.directory,
                turn.folders,
            )
        except (OSError, RuntimeError):
            return  # Sending reports the problem; a pre-start stays silent
        with self.warm_lock:
            if self.warm_turn != turn or self.warm is not None:
                return
            with self.process_lock:
                if self.closed.is_set():
                    return
                try:
                    process = self._spawn(command, cwd)
                except OSError:
                    return
            self.warm = (command, cwd, process)
            # Close an unused CLI after a while instead of keeping it for the rest of the session
            self.warm_timer = threading.Timer(WARM_IDLE, self._expire_warm, (process,))
            self.warm_timer.daemon = True
            self.warm_timer.start()

    def _take_warm(self, command: list[str], cwd: Path) -> subprocess.Popen[str] | None:
        """Claim the pre-started CLI when it matches this turn, and close it otherwise.

        Args:
            command (list[str]): The command line this turn needs.
            cwd (Path): The working directory this turn needs.

        Returns:
            subprocess.Popen[str] | None: The waiting process, or None to start a new one.
        """
        with self.warm_lock:
            warm, self.warm = self.warm, None
            self.warm_turn = None
            if self.warm_timer is not None:
                self.warm_timer.cancel()
                self.warm_timer = None
        if warm is None:
            return None
        if warm[0] == command and warm[1] == cwd and warm[2].poll() is None:
            return warm[2]
        self._reap(warm[2])
        return None

    def _expire_warm(self, process: subprocess.Popen[str]) -> None:
        """Close a pre-started CLI that waited too long.

        Args:
            process (subprocess.Popen[str]): The process the idle timer was set for.
        """
        with self.warm_lock:
            if self.warm is not None and self.warm[2] is process:
                self.warm_turn = None
                self._discard_warm()

    def _discard_warm(self) -> None:
        """Close the pre-started CLI and cancel any pending start; call with the warm lock held."""
        if self.warm_timer is not None:
            self.warm_timer.cancel()
            self.warm_timer = None
        if self.warm is not None:
            self._reap(self.warm[2])
            self.warm = None

    def _reap(self, process: subprocess.Popen[str]) -> None:
        """Terminate an unused CLI and release it on a background thread.

        Args:
            process (subprocess.Popen[str]): The process to close.
        """
        try:
            process.terminate()
        except OSError:
            pass
        threading.Thread(target=self._release, args=(process,), daemon=True).start()

    def _release(self, process: subprocess.Popen[str]) -> None:
        """Wait for a terminated CLI to exit, then close its pipes and stop tracking it.

        Args:
            process (subprocess.Popen[str]): The terminated process.
        """
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._close_pipes(process)
        with self.process_lock:
            self.processes.discard(process)

    @staticmethod
    def _close_pipes(process: subprocess.Popen[str]) -> None:
        """Close a finished process's pipes.

        Args:
            process (subprocess.Popen[str]): The finished process.
        """
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

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


def _tool_status(block: dict[str, Any]) -> str:
    """Describe a tool call in a few words while the reply is being written.

    Args:
        block (dict[str, Any]): The tool use content block.

    Returns:
        str: A short status such as "Reading src/app.py…".
    """
    tool = str(block.get("name") or "a tool")
    tool_input = block.get("input")
    values: dict[str, Any] = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Read" and values.get("file_path"):
        return f"Reading {'/'.join(Path(str(values['file_path'])).parts[-2:])}…"
    if tool == "Grep" and values.get("pattern"):
        return f"Searching for {str(values['pattern'])[:60]}…"
    if tool == "Glob" and values.get("pattern"):
        return f"Finding {str(values['pattern'])[:60]}…"
    return f"Using {tool}…"
