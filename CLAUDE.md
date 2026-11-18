This file provides guidance to Claude Code when working with code in this repository.

There may be other agents working at the same time as you. Be cognisant of this when making changes and performing git operations. Do not overwrite other agents' changes.

When making changes, ask me any questions you need about my priorities, preferences, purpose, etc. Feel free to discuss trade-offs with different implementation approaches, alternative plans, etc. Ensure your changes DO NOT ALTER ANY OTHER pre-existing functionality - that is, everything that was working before the change should continue working after the change.

Feel free to run any tests you need.

## Project

Que is a Windows-only stealth AI programming assistant — a frameless, translucent PyQt6 overlay that captures screenshots and streams Gemini API responses. It uses Win32 low-level keyboard hooks to register global hotkeys invisibly.

## Commands

```bash
# Run from repo root with venv active
python src/que.py

# Static analysis (must all pass before merging)
ruff check src test native
ruff format --check src test native
mypy --strict src test native
```

## Architecture

```
src/Que.py — Entry point; wires together all components
src/core/ — Backend: AI, input hooks, screenshots
src/ui/ — PyQt6 overlay UI (frameless, translucent)
src/data/ — Runtime data (chat_history.json, config.json, cache/)
src/assets/ — Icons and images
```

**Data flow:** User hotkey → `ShortcutManager` emits Qt signal → `AIReceiver.handle_message()` → `threading.Thread` calls `AISender.generate_content_with_screenshot()` → Gemini streaming API → chunks emitted via `pyqtSignal` → `ChatArea` renders via `ChatBubble` + `ai_formatter.py`.

**Key components:**
- `src/que.py` — Entry point; wires all components together
- `core/shortcut_manager.py` — Runs hotkey actions on the Qt thread; the physical keys are decided by the input helper
- `core/hook_engine.py` / `core/hook_host.py` — Qt-free keyboard/mouse decision logic and the input helper process (`que.py --hook-host`) that hosts the `WH_KEYBOARD_LL`/`WH_MOUSE_LL` hooks plus Raw Input leak detection, so Que's own work can never stall the hooks
- `core/native_hook.py` + `native/que_hook.c` — The hook callbacks in compiled code (`src/assets/que_hook.dll`, committed; rebuild with `python native/build.py`): a C port of `HookEngine`'s rules on its own native thread with locked memory, so no Python work can delay a callback. `HookEngine` stays the reference; `test/test_native_hook.py` requires identical decisions. Policy tables (`HOTKEYS` etc.) are passed in from `core/hook_engine.py`, never duplicated in C.
- `core/hook_client.py` — Starts and supervises the helper over authenticated named pipes; turns its events into Qt signals; when the "Allow shortcuts in admin apps" setting is on (default off, read once at startup), starts it as administrator with one UAC prompt per launch so hooks also see input to elevated windows like Task Manager; declined prompts and restarts use an unelevated helper
- `core/window_policy.py` — Applies capture affinity and non-activating, click-through styles to every top-level window before it is shown
- `ui/input/input_router.py` — Delivers routed keys and clicks to Qt widgets without activating Que's windows
- `core/win32_hook.py` — ctypes bindings for Win32 keyboard hook constants and types
- `core/ai_sender.py` — Gemini API client using `google-genai` with streaming; supports file attachments and thinking mode
- `core/ai_receiver.py` — Bridges background `threading.Thread` AI generation with UI via `pyqtSignal`
- `ui/window/main_window.py` — Frameless translucent `QWidget` with `WindowStaysOnTopHint | Tool`; re-raises itself every 1s via visibility timer
- `ui/chat/ai_formatter.py` — Markdown→HTML converter for `QLabel`; known pain point, being redesigned (see WIP.md)
- `ui/theme.py` — Centralized color theming; all colors must come from here
- `src/data/` — Runtime data: `chat_history.json`, `config.json`, `cache/`

## Threading Rules

**UI work must happen on the main Qt thread.** The input-helper pipe reader thread and AI generation thread communicate with UI exclusively through `pyqtSignal`. Never call UI methods directly from a non-main thread.

Use `threading.Thread(daemon=True)` for background work — not `QThread` (Nuitka compatibility issue).

## Translucency Rules

The window background is one translucent fill (`BG_COLOR` at `WINDOW_ALPHA`, painted by `MainWindow._paint_frame`). Translucent fills compound, so any widget that paints its own translucent background inside the window looks darker than the window. This bug has recurred (history sidebar, model dropdowns, transcript panel); avoid it:
- Never give an in-window widget a translucent background fill. Use `background: transparent` and a border for framing (see `InputField`'s input container).
- Dock new panels in the layout and grow the window by their height (see `InputField` and `TranscriptPanel`) instead of floating them over the chat, so they never cover text and need no fill.
- If a widget must float over content, clear its area and repaint the window frame instead of filling over it (`CompositionMode_Source` with transparent, then `MainWindow._paint_frame`; see `HistorySidebar.paintEvent`).

## Hotkey System

Hotkeys are declared once in `core/hook_engine.py` `HOTKEYS` as `(modifier_bitmask, vk_code, action_name, repeat_allowed, always_active)`; `HookEngine` builds two tables from it:
- `always_active_hotkeys` — active even when overlay is hidden
- `main_window_hotkeys` — only active when overlay is visible

The helper sends the action name to Que, and `ShortcutManager` runs its `_<action_name>` method.

All matched key events are suppressed (return 1 from hook proc).

Every Que shortcut starts with **Caps Lock** (`QUE_KEY`, chord bit `MOD_QUE`), Que's dedicated key: it never toggles while Que runs (the helper turns Caps Lock off at startup if it is on), and it, any modifier pressed while it is held and any key pressed with it are withheld from other apps, so a page sees nothing of a Que shortcut. Caps Lock + a non-Que key is swallowed, never replayed. All other modifiers (Left and Right Ctrl, Shift, Alt, Win) pass through at once with natural timing and auto-repeat; holding them back made Que detectable from ordinary typing. While typing in Que, all keys including modifiers go to Que.

## Coding Style Conventions

### Import Statements
All import statements must follow PEP 8 standard import ordering:
1. Standard library imports
2. Third-party imports
3. Local application imports

Separate each group with exactly one blank line. Sort imports alphabetically within each group. Within each group, bare `import X` statements come before `from X import Y` statements. When importing multiple names from a single module, list them alphabetically on one line:
```python
from PyQt6.QtCore import QObject, QPoint, QTimer, pyqtSignal
```
When the list is long, use a parenthesized multi-line form with one name per line, alphabetized:
```python
from core.win32_hook import (
    HOOKPROC,
    KBDLLHOOKSTRUCT,
    MOD_ALT,
    MOD_CTRL,
)
```
Use **absolute imports** (ex. `from core.ai_receiver import AIReceiver`) for all imports, regardless of whether it is in the same module or cross-module.
Never use wildcard/star imports.

### Class and Method Ordering
All public classes and public methods *must* be defined before private classes and private methods. So, all classes and methods that begin with an underscore *must* be defined after all classes and methods that do not have a name that begins with an underscore.
Additionally, keep related functions/methods together, e.g., all GUI setup functions together.

### Docstrings
Every function (except __init__), method, and class must have a Google-style docstring using triple double quotes (`"""`). All docstrings should be in imperative mood and every sentence should end with a period.

**One-line docstrings:** Opening and closing quotes on the same line if the docstring fits in a single line.
```python
def stop(self):
    """Signal the thread to stop."""
```

**Multi-line docstrings:** Opening triple quotes on the same line as a short summary. Blank line after summary before sections. Closing triple quotes on their own line, aligned with the enclosing definition.

**Sections**:
- `Args:` — List parameters with type in parentheses and description. Do not add " | None" if the parameter is a Qt object (all Qt objects can be None).Omit if there are no parameters.
- `Returns:` — Describe the return value with type and description. Omit if `None`.

Do not document unused parameters required only for overriding methods. 

Example:
```python
def generate_content(self, user_input, on_chunk=None):
    """Generate AI content by streaming from the Gemini model.

    Args:
        user_input (str): The user's input text to send to the model.
        on_chunk (callable, optional): Callback invoked with each text chunk as it streams.

    Returns:
        str: The full generated response text.
    """
```

**Class docstrings** are typically single-line. For complex classes, use a multi-line docstring with a blank line separating the summary from the extended description:
```python
class ShortcutManager(QObject):
    """Manages global keyboard shortcuts via a Win32 low-level keyboard hook.

    All registered hotkeys are suppressed so other applications never see the
    key events.
    """
```

### Type Hints
Always add type hints, return types, and function annotations to all functions and methods. Use `-> None` for functions that do not return a value. If the type of a parameter or return value is unclear, use `Any` from the `typing` module. If there are multiple possible types, use `|` to indicate a union (e.g. `str | None`). For callbacks, use `Callable` from `collections.abc` (never the built-in `callable`) with the appropriate signature (e.g. `Callable[[str], None]` for a function that takes a string and returns nothing).

**Strict mypy requirements (`mypy --strict`):**
- Generic types must always include type parameters: use `re.Match[str]`, not `re.Match`; use `list[str]`, not `list`; etc.
- Never use `callable` as a type — always import and use `Callable` from `collections.abc`.
- PyQt6 event override methods must accept `| None` on the event parameter to match the supertype signature, e.g.:
  ```python
  def mousePressEvent(self, event: QMouseEvent | None) -> None:
  def paintEvent(self, _event: QPaintEvent | None) -> None:
  def resizeEvent(self, event: QResizeEvent | None) -> None:
  def enterEvent(self, event: QEnterEvent | None) -> None:
  def leaveEvent(self, event: QEvent | None) -> None:
  ```
- When calling Qt methods that return `X | None` (e.g. `QApplication.primaryScreen()`, `verticalScrollBar()`), either assert the result is not `None` or store it typed as `X` before use. When possible, directly instantiate objects in __init__ instead of setting the variable as `None` in __init__. This ensures the variable is always properly typed and eliminates the need for `None` checks.
- Functions that return a value from a C-extension call (e.g. ctypes/win32) must explicitly cast the result to the declared return type (e.g. `return int(...)`) to avoid returning `Any`.

Example:
```python
def process(a: int, b: int) -> None:
```

When passing in an object as a parameter, the parameter name should be the class name in lower_snake_case and the type hint should be the class of the object. You should use the most general type that covers the operations needed. Do not over-specify unless subclass-specific functionality of the parameter is required (ex. accessing a parameter object's methods/variables).

Example:
```python
def __init__(self, main_window: QWidget, bg_color: QColor) -> None:  # main_window is only used for super.__init__ and therefore does not need to be typed as MainWindow
```
```python
def __init__(self, chat_area: ChatArea, is_top: bool) -> None:  # chat_area.bg_color is accessed later in the method, which falls under subclass-specific functionality, and therefore chat_area should be typed as ChatArea, not QWidget
```

### Naming Conventions

| Category | Convention | Examples |
|---|---|---|
| Classes | `PascalCase` | `MainWindow`, `ChatBubble`, `AIReceiver` |
| Public Functions / Methods | `snake_case` | `take_screenshot`, `handle_message` |
| Private Functions / Methods | Single underscore prefix | `_initUI`, `_on_response_chunk` |
| Public Variables | `snake_case` | `full_response`, `chat_history_path` |
| Private Variables | No prefix | `streaming_bubble`, `visibility_timer` | 
| Public Constants | `UPPER_SNAKE_CASE` | `WH_KEYBOARD_LL`, `MOD_CTRL` |
| Private Constants | No prefix | `BG_COLOR`, `VK_SHIFT` |
| Qt Signals | `snake_case`, no underscore | `message_sent`, `move_signal`, `finished` |
| Event Handler Methods | `on_` prefix | `on_response_ready`, `on_tray_activated` |

Acronyms stay uppercase in class names: `AISender`, `AIReceiver`. No double-underscore (name-mangling) prefixes.

Unused variables and parameters (in functions that are overriden, for example) should be prefixed with an underscore to indicate intentional non-use (e.g. `_unused_param`).

### String Formatting
- Use **f-strings** for all string interpolation. Never use `.format()` or `%`-style formatting.
- Use **single quotes** (`'...'`) only when the outside string uses double quotes.
- Use **double quotes** (`"..."`) for all regular strings.
- Use **triple double quotes** (`"""..."""`) for docstrings and multi-line prompt strings.
- Never use **triple single quotes**.


### Whitespace and Formatting
- **Indentation:** 4 spaces.
- **Blank lines:** 2 blank lines before top-level class/function definitions. 1 blank line between methods within a class while respecting the surrounding indentation. 1 blank line between logical blocks inside methods while respecting the surrounding indentation.
- **Line length:** No strict limit, but keep lines reasonable (~120 characters). Long expressions may span multiple lines.
- **Multi-line expressions:** Opening paren at end of line, closing paren on its own line aligned with the statement. Operators at end of line:
```python
self.setWindowFlags(
    Qt.WindowType.FramelessWindowHint |
    Qt.WindowType.WindowStaysOnTopHint |
    Qt.WindowType.Tool
)
```

### Inline Comments
- Separated by at least 2 spaces from code. Use sparingly for non-obvious logic.

### Commit Conventions
Never add yourself (Claude) to the commit or attributions to any coding agents.
