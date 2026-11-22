"""Make coding agents run the full CI before they finish a turn that changed the repository.

Claude Code (.claude/settings.json) and Codex (.codex/hooks.json) run this as hooks, passing the
hook's JSON on stdin:
    pre / post  Around each tool call, note whether that call changed tracked or new files.
    stop        Block finishing until a full `python test/run_ci.py` has started since the last change.

Several agents share one working tree, so a change counts against an agent only when it happens
during that agent's own tool call. A full CI run started by any agent counts, because it tests the
shared tree. State lives in .git/que-ci, so it is never committed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
STATE = (
    REPOSITORY / ".git" / "que-ci"
)  # shortcut: assumes a normal clone, not a git worktree.
LAST_RUN = STATE / "last-run.json"
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch"}
SHELL_TOOLS = {"Bash", "PowerShell"}
MAX_BLOCKS = 3  # After this many refusals in a row the agent may stop, so a broken setup cannot trap it.


def main() -> int:
    """Run the hook named on the command line.

    Returns:
        int: 2 to block the agent from finishing (reason on stderr), otherwise 0.
    """
    try:
        event: dict[str, Any] = json.load(sys.stdin)
        session = str(event.get("session_id") or "unknown")
        if sys.argv[1] == "pre":
            _write(STATE / "pre" / _call_id(event), {"tree": _tree()})
        elif sys.argv[1] == "post":
            _on_tool_finished(event, session)
        elif sys.argv[1] == "stop":
            return _on_stop(session)
    except Exception as error:  # noqa: BLE001  # A broken gate must never break the agent's tools.
        print(f"agent_ci_gate: {error!r}", file=sys.stderr)
    return 0


def record_ci_run(started: float, passed: bool) -> None:
    """Record that a full local CI run finished, for the stop gate.

    Args:
        started (float): The run's start time in seconds since the epoch.
        passed (bool): Whether every step passed.
    """
    _write(LAST_RUN, {"started": started, "passed": passed})


def stop_reason(
    last_change: float | None,
    last_run: dict[str, Any] | None,
    blocks: int,
    warned_run: float | None,
) -> str | None:
    """Decide whether an agent may finish its turn.

    Args:
        last_change (float | None): When this agent last changed files, or None if it never did.
        last_run (dict[str, Any] | None): The last full CI run's start time and result, if any.
        blocks (int): How many times in a row this agent has already been blocked.
        warned_run (float | None): The start time of the failed run this agent was already told about.

    Returns:
        str | None: Why the agent must keep working, or None if it may finish.
    """
    if last_change is None or blocks >= MAX_BLOCKS:
        return None
    if last_run is None or float(last_run["started"]) < last_change:
        return (
            "You changed files in this repository but have not run the full CI since. Run "
            "`python test/run_ci.py` (about 7 minutes), fix any failures your change caused, and report "
            "its summary. If a failure is unrelated to your change, say so instead of hiding it."
        )
    if not last_run["passed"] and last_run["started"] != warned_run:
        return (
            "The last full CI run failed. Fix the failures your change caused and rerun "
            "`python test/run_ci.py`, or, if they are unrelated to your change (compare with "
            "`gh run list --branch main`), say so in your reply."
        )
    return None


def _on_tool_finished(event: dict[str, Any], session: str) -> None:
    """Note a change if this tool call edited files.

    Args:
        event (dict[str, Any]): The PostToolUse hook input.
        session (str): The agent's session id.
    """
    tool = str(event.get("tool_name", ""))
    before = _read(STATE / "pre" / _call_id(event))
    (STATE / "pre" / _call_id(event)).unlink(missing_ok=True)
    if tool in SHELL_TOOLS:
        command = str((event.get("tool_input") or {}).get("command", ""))
        # Other agents keep editing during a long CI run; that must not count against this one.
        if "run_ci.py" in command or before is None or before["tree"] == _tree():
            return
    elif tool not in EDIT_TOOLS:
        return
    _write(
        STATE / "sessions" / f"{session}.json",
        {"last_change": time.time(), "blocks": 0},
    )


def _on_stop(session: str) -> int:
    """Block the agent from finishing if it has not run CI since its last change.

    Args:
        session (str): The agent's session id.

    Returns:
        int: 2 to block, otherwise 0.
    """
    path = STATE / "sessions" / f"{session}.json"
    state = _read(path)
    if state is None:
        return 0
    last_run = _read(LAST_RUN)
    reason = stop_reason(
        state["last_change"], last_run, state["blocks"], state.get("warned_run")
    )
    if reason is None:
        return 0
    warned_run = last_run["started"] if last_run and not last_run["passed"] else None
    _write(path, {**state, "blocks": state["blocks"] + 1, "warned_run": warned_run})
    print(reason, file=sys.stderr)
    return 2


def _call_id(event: dict[str, Any]) -> str:
    """Return a file-name-safe id pairing a tool call's pre and post hooks.

    Args:
        event (dict[str, Any]): The hook input.

    Returns:
        str: The id.
    """
    raw = str(event.get("tool_use_id") or event.get("session_id") or "unknown")
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in raw
    )


def _tree() -> str:
    """Hash the working tree's tracked and untracked, non-ignored files without touching the real index.

    Returns:
        str: The git tree id of the working tree's contents.
    """
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / "index"
        if (REPOSITORY / ".git" / "index").exists():
            shutil.copyfile(
                REPOSITORY / ".git" / "index", index
            )  # Reuse its stat cache so only changes are hashed.
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        subprocess.run(
            ["git", "add", "-A"],
            cwd=REPOSITORY,
            env=env,
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["git", "write-tree"],
            cwd=REPOSITORY,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


def _read(path: Path) -> dict[str, Any] | None:
    """Read a JSON state file.

    Args:
        path (Path): The file.

    Returns:
        dict[str, Any] | None: Its contents, or None if it does not exist or is unreadable.
    """
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON state file atomically, so concurrent agents never read half of one.

    Args:
        path (Path): The file.
        data (dict[str, Any]): The contents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


if __name__ == "__main__":
    sys.exit(main())
