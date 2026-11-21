import html
import re
from functools import cache

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name
from pygments.token import Token, _TokenType, is_token_subtype
from pygments.util import ClassNotFound

from ui.chat.latex import render_math
from ui.theme import (
    BRACKET_COLORS,
    CODE_BG_ALPHA,
    CODE_COLORS,
    LINK_COLOR,
    PRIMARY_COLOR,
    qss,
)

BODY_FONT = "Segoe UI"
CODE_FONT = "Cascadia Mono, Consolas, monospace"
BODY_PT = 10.5
CODE_PT = 9.5
BLOCK_GAP = 10  # Pixels between paragraphs, lists, code blocks and tables
ITEM_GAP = 4  # Pixels between list items
HEADING_GAP = 4  # Pixels under a heading, so it sits with the section it introduces
HEADING_STYLES = {  # Level: (point size, pixels above)
    1: (14.0, 18),
    2: (12.5, 16),
    3: (11.5, 14),
    4: (BODY_PT, 12),
    5: (BODY_PT, 12),
    6: (BODY_PT, 12),
}
MUTED = qss(PRIMARY_COLOR, 200)
RULE = qss(PRIMARY_COLOR, 60)
CODE_BG = qss((0, 0, 0), CODE_BG_ALPHA)
LINK_OPEN = f"<span style='color: {qss(LINK_COLOR)};'>"
BULLETS = ("•", "◦", "▪")

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`]*)[^`]*$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
TASK_RE = re.compile(r"^\[([ xX])\]\s+")
DISPLAY_MATH_RE = re.compile(r"^\s*(\$\$|\\\[)(.*)$")

PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")
ESCAPED_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>$])")
CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
# A lone $ opens maths only before a non-space and closes only after one and not before a digit, so prices stay text
INLINE_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$|\\\((.+?)\\\)|\\\[(.+?)\\\]|(?<![\\$\w])\$(?=[^\s$])(.+?)(?<=[^\s\\])\$(?![\d$\w])"
)
LINK_RE = re.compile(r"\[([^\]\n]+)\]\([^)\s]+(?:\s+\"[^)]*\")?\)")
LINE_BREAK_TAG_RE = re.compile(r"&lt;br\s*/?&gt;", re.IGNORECASE)
EMPHASIS_RULES = (
    (re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"), r"<b><i>\1</i></b>"),
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), r"<b>\1</b>"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)"), r"<i>\1</i>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"<s>\1</s>"),
)

# Pygments lumps these in with other keywords, but VS Code colours flow control apart
CONTROL_KEYWORDS = frozenset(
    [
        "as",
        "await",
        "break",
        "case",
        "catch",
        "continue",
        "default",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "except",
        "export",
        "fi",
        "finally",
        "for",
        "from",
        "goto",
        "if",
        "import",
        "in",
        "include",
        "loop",
        "match",
        "pass",
        "raise",
        "return",
        "switch",
        "then",
        "throw",
        "try",
        "using",
        "while",
        "with",
        "yield",
    ]
)
TOKEN_ROLES: dict[_TokenType, str] = {
    Token.Comment: "comment",
    Token.Comment.Preproc: "control",
    Token.Comment.PreprocFile: "string",
    Token.Keyword: "keyword",
    Token.Keyword.Namespace: "control",
    Token.Name.Attribute: "variable",
    Token.Name.Builtin.Pseudo: "keyword",
    Token.Name.Class: "type",
    Token.Name.Constant: "constant",
    Token.Name.Decorator: "function",
    Token.Name.Entity: "keyword",
    Token.Name.Exception: "type",
    Token.Name.Function: "function",
    Token.Name.Namespace: "type",
    Token.Name.Tag: "keyword",
    Token.Name.Variable: "variable",
    Token.Literal: "number",
    Token.Literal.String: "string",
    Token.Literal.String.Affix: "keyword",
    Token.Literal.String.Escape: "escape",
    Token.Literal.String.Interpol: "keyword",
    Token.Literal.String.Regex: "regex",
    Token.Operator: "operator",
    Token.Operator.Word: "keyword",
    Token.Generic.Deleted: "string",
    Token.Generic.Heading: "keyword",
    Token.Generic.Inserted: "number",
    Token.Generic.Prompt: "keyword",
    Token.Generic.Subheading: "keyword",
}


def format_message(message: str) -> str:
    """Convert a Markdown message from any model into rich text for a QLabel.

    Handles headings, paragraphs, nested and task lists, block quotes, tables, rules, fenced code
    with syntax highlighting, and inline bold, italic, strikethrough, code and links. Unclosed
    fences and half-written lines render sensibly so a streaming reply can be shown at every chunk.

    Args:
        message (str): The raw Markdown message text to format.

    Returns:
        str: HTML-formatted message string ready for a QLabel.
    """
    lines = message.replace("\r\n", "\n").expandtabs(4).split("\n")
    return (
        f"<div style='font-family: {BODY_FONT}; font-size: {BODY_PT}pt; white-space: pre-wrap;'>"
        f"{_render_blocks(lines, BLOCK_GAP)}</div>"
    )


def _render_blocks(lines: list[str], gap: int, depth: int = 0) -> str:
    """Render a run of Markdown lines as a sequence of block elements.

    Args:
        lines (list[str]): The lines to render, already stripped of any enclosing indentation.
        gap (int): Pixels between consecutive blocks.
        depth (int): How many lists enclose the lines.

    Returns:
        str: The blocks as rich text.
    """
    blocks: list[str] = []
    after_heading = False
    i = 0
    while i < len(lines):
        line = lines[i]
        top = (HEADING_GAP if after_heading else gap) if blocks else 0
        after_heading = bool(HEADING_RE.match(line)) or (
            after_heading and not line.strip()
        )
        if not line.strip():
            i += 1
        elif fence := FENCE_RE.match(line):
            indent, marker, language = fence.groups()
            closing = re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")
            code: list[str] = []
            i += 1
            while i < len(lines) and not closing.match(lines[i]):
                code.append(_dedent(lines[i], len(indent)))
                i += 1
            i += 1  # Skip the closing fence; a stream may not have sent it yet
            blocks.append(_code_block("\n".join(code), language, top))
        elif math := _display_math(line):
            closer = "$$" if math.group(1) == "$$" else "\\]"
            body = [math.group(2)]
            i += 1
            while closer not in body[-1] and i < len(lines):
                body.append(lines[i])
                i += 1
            tex = "\n".join(body).split(closer)[
                0
            ]  # A stream may not have sent the closer yet
            blocks.append(
                f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-top: {top}px;'>"
                f"<tr><td align='center'>{render_math(tex, display=True)}</td></tr></table>"
            )
        elif heading := HEADING_RE.match(line):
            level = len(heading.group(1))
            size, heading_gap = HEADING_STYLES[level]
            blocks.append(
                f"<p style='margin: {heading_gap if blocks else 0}px 0 0 0; font-size: {size}pt; "
                f"font-weight: bold;'>{_inline(heading.group(2))}</p>"
            )
            i += 1
        elif RULE_RE.match(line):
            blocks.append(
                f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-top: {top}px;'>"
                f"<tr><td style='background-color: {RULE}; font-size: 1pt;'>&nbsp;</td></tr></table>"
            )
            i += 1
        elif QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines) and (quote := QUOTE_RE.match(lines[i])):
                quoted.append(quote.group(1))
                i += 1
            blocks.append(
                f"<table cellspacing='0' cellpadding='0' style='margin-top: {top}px;'><tr>"
                f"<td width='3' style='background-color: {RULE}; font-size: 1pt;'>&nbsp;</td>"
                f"<td style='padding-left: 10px; color: {MUTED};'>{_render_blocks(quoted, gap, depth)}</td>"
                "</tr></table>"
            )
        elif LIST_RE.match(line):
            items, i = _collect_list(lines, i)
            blocks.append(_list(items, top, depth))
        elif _is_table_start(lines, i):
            rows, separator = [lines[i]], lines[i + 1]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows, separator, top))
        else:
            paragraph = [line.strip()]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines, i):
                paragraph.append(lines[i].strip())
                i += 1
            blocks.append(
                f"<p style='margin: {top}px 0 0 0;'>{'<br>'.join(_inline(text) for text in paragraph)}</p>"
            )
    return "".join(blocks)


def _starts_block(lines: list[str], i: int) -> bool:
    """Check whether a line begins a block other than a paragraph.

    Args:
        lines (list[str]): The lines being rendered.
        i (int): The index of the line to check.

    Returns:
        bool: Whether the line interrupts a paragraph.
    """
    line = lines[i]
    return bool(
        FENCE_RE.match(line)
        or _display_math(line)
        or HEADING_RE.match(line)
        or RULE_RE.match(line)
        or QUOTE_RE.match(line)
        or LIST_RE.match(line)
        or _is_table_start(lines, i)
    )


def _display_math(line: str) -> re.Match[str] | None:
    """Match a line that opens display maths on its own, rather than inline maths in a sentence.

    Args:
        line (str): The line to check.

    Returns:
        re.Match[str] | None: The opening delimiter and the rest of the line, or None.
    """
    math = DISPLAY_MATH_RE.match(line)
    if math is None:
        return None
    closer = "$$" if math.group(1) == "$$" else "\\]"
    rest = math.group(2).rstrip()
    return (
        math
        if closer not in rest or (rest.endswith(closer) and rest.count(closer) == 1)
        else None
    )


def _is_table_start(lines: list[str], i: int) -> bool:
    """Check whether a line is a table header row followed by its separator row.

    Args:
        lines (list[str]): The lines being rendered.
        i (int): The index of the candidate header row.

    Returns:
        bool: Whether a table starts at the line.
    """
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and "|" in lines[i + 1]
        and bool(TABLE_SEPARATOR_RE.match(lines[i + 1]))
    )


def _dedent(line: str, width: int) -> str:
    """Remove up to a number of leading spaces from a line.

    Args:
        line (str): The line to dedent.
        width (int): The most spaces to remove.

    Returns:
        str: The dedented line.
    """
    return line[min(width, len(line) - len(line.lstrip(" "))) :]


def _collect_list(
    lines: list[str], start: int
) -> tuple[list[tuple[str, list[str]]], int]:
    """Gather the items of the list that starts at a line, each with its dedented content lines.

    Args:
        lines (list[str]): The lines being rendered.
        start (int): The index of the list's first item.

    Returns:
        tuple[list[tuple[str, list[str]]], int]: Each item's marker and content lines, and the
            index of the first line after the list.
    """
    first = LIST_RE.match(lines[start])
    assert first is not None
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    items: list[tuple[str, list[str]]] = []
    content_indent = 0
    i = start
    while i < len(lines):
        line = lines[i]
        line_indent = len(line) - len(line.lstrip(" "))
        item = LIST_RE.match(line)
        if item and line_indent < indent + 2:
            if item.group(2)[0].isdigit() != ordered:
                break  # A different kind of list starts here
            items.append((item.group(2), [item.group(3)]))
            content_indent = line_indent + len(item.group(2)) + 1
        elif not line.strip():
            following = next((text for text in lines[i + 1 :] if text.strip()), "")
            following_indent = len(following) - len(following.lstrip(" "))
            if not following or (
                following_indent < indent + 2 and not LIST_RE.match(following)
            ):
                break
            items[-1][1].append("")
        elif line_indent >= indent + 2 or (
            items[-1][1][-1].strip() and not _starts_block(lines, i)
        ):
            # Indented content belongs to the item; so does an unindented line that continues its text
            items[-1][1].append(_dedent(line, content_indent))
        else:
            break
        i += 1
    return items, i


def _list(items: list[tuple[str, list[str]]], top: int, depth: int) -> str:
    """Render list items as a table so wrapped lines hang under the text, not the marker.

    Ordered items are numbered on from the first item's number, as Markdown does.

    Args:
        items (list[tuple[str, list[str]]]): Each item's marker and content lines.
        top (int): Pixels above the list.
        depth (int): How many lists enclose this one.

    Returns:
        str: The list as rich text.
    """
    ordered = items[0][0][0].isdigit()
    start = int(items[0][0][:-1]) if ordered else 0
    width = 10 + 8 * len(str(start + len(items))) if ordered else 18
    rows: list[str] = []
    for index, (marker, content) in enumerate(items):
        if task := TASK_RE.match(content[0]):
            marker = "☐" if task.group(1) == " " else "☑"
            content = [content[0][task.end() :], *content[1:]]
        elif ordered:
            marker = f"{start + index}{marker[-1]}"
        else:
            marker = BULLETS[depth % len(BULLETS)]
        padding = ITEM_GAP if index else 0
        rows.append(
            f"<tr><td width='{width}' align='right' valign='top' "
            f"style='padding: {padding}px 6px 0 0;{'' if ordered else f' color: {MUTED};'}'>{marker}</td>"
            f"<td valign='top' style='padding-top: {padding}px;'>{_render_blocks(content, ITEM_GAP + 2, depth + 1)}</td></tr>"
        )
    return f"<table cellspacing='0' cellpadding='0' style='margin-top: {top}px;'>{''.join(rows)}</table>"


def _table(rows: list[str], separator: str, top: int) -> str:
    """Render a GitHub-style table with a shaded header row and its column alignments.

    Args:
        rows (list[str]): The header row followed by the body rows.
        separator (str): The separator row, whose colons set each column's alignment.
        top (int): Pixels above the table.

    Returns:
        str: The table as rich text.
    """
    alignments = [
        "center"
        if cell.startswith(":") and cell.endswith(":")
        else "right"
        if cell.endswith(":")
        else "left"
        for cell in _cells(separator)
    ]
    html_rows: list[str] = []
    for index, row in enumerate(rows):
        cells: list[str] = []
        for column, cell in enumerate(_cells(row)):
            align = alignments[column] if column < len(alignments) else "left"
            style = f"padding: 4px 10px; border: 1px solid {RULE};"
            if index == 0:
                style += f" background-color: {CODE_BG}; font-weight: 600;"
            cells.append(
                f"<td align='{align}' valign='top' style='{style}'>{_inline(cell)}</td>"
            )
        html_rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f"<table cellspacing='0' cellpadding='0' style='margin-top: {top}px; border-collapse: collapse;'>"
        f"{''.join(html_rows)}</table>"
    )


def _cells(row: str) -> list[str]:
    """Split a table row into its trimmed cells, keeping escaped pipes inside cells.

    Args:
        row (str): The table row.

    Returns:
        list[str]: The cell texts.
    """
    row = row.strip()
    row = row.removeprefix("|")
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def _inline(text: str) -> str:
    """Render inline Markdown (code, maths, links, emphasis) in one line of text.

    Args:
        text (str): The raw text.

    Returns:
        str: The text as rich text.
    """
    protected: list[str] = []

    def protect(fragment: str) -> str:
        """Hide a finished fragment from later rules behind a placeholder.

        Args:
            fragment (str): The rich text to hide.

        Returns:
            str: The placeholder.
        """
        protected.append(fragment)
        return f"\x00{len(protected) - 1}\x00"

    text = CODE_SPAN_RE.sub(
        lambda match: protect(
            f"<span style='font-family: {CODE_FONT}; font-size: {CODE_PT}pt; color: {qss(CODE_COLORS['text'])}; "
            f"background-color: {CODE_BG};'>{html.escape(match.group(2).strip(), quote=False)}</span>"
        ),
        text,
    )
    text = INLINE_MATH_RE.sub(
        lambda match: protect(
            render_math(
                next(tex for tex in match.groups() if tex is not None), display=False
            )
        ),
        text,
    )
    text = ESCAPED_RE.sub(
        lambda match: protect(html.escape(match.group(1), quote=False)), text
    )
    text = html.escape(text, quote=False)
    text = LINE_BREAK_TAG_RE.sub(lambda _match: protect("<br>"), text)
    # Links show only their coloured text: the overlay never opens them or changes the cursor over them
    text = LINK_RE.sub(
        lambda match: f"{protect(LINK_OPEN)}{match.group(1)}</span>",
        text,
    )
    for pattern, replacement in EMPHASIS_RULES:
        text = pattern.sub(replacement, text)
    return PLACEHOLDER_RE.sub(lambda match: protected[int(match.group(1))], text)


def _code_block(code: str, language: str, top: int) -> str:
    """Render a fenced code block on a darker panel, with its language above the code.

    Args:
        code (str): The code inside the fence.
        language (str): The language named on the opening fence, or an empty string.
        top (int): Pixels above the block.

    Returns:
        str: The code block as rich text.
    """
    header = (
        f"<tr><td style='padding: 6px 12px 0 12px; font-size: 8.5pt; color: {MUTED};'>"
        f"{html.escape(language)}</td></tr>"
        if language
        else ""
    )
    return (
        f"<table width='100%' cellspacing='0' cellpadding='0' "
        f"style='margin-top: {top}px; background-color: {CODE_BG};'>{header}"
        f"<tr><td style='padding: {4 if language else 8}px 12px 8px 12px;'>"
        f"<pre style='margin: 0; font-family: {CODE_FONT}; font-size: {CODE_PT}pt; "
        f"color: {qss(CODE_COLORS['text'])}; white-space: pre-wrap;'>{_highlight(code, language)}</pre>"
        "</td></tr></table>"
    )


def _highlight(code: str, language: str) -> str:
    """Colour code the way VS Code's Dark Modern theme does, including bracket pairs.

    Args:
        code (str): The code to colour.
        language (str): The language name or alias, or an empty string for plain text.

    Returns:
        str: The code as rich text.
    """
    lexer = _lexer(language.lower())
    if lexer is None:
        return html.escape(code, quote=False)
    tokens = list(lexer.get_tokens(code))
    parts: list[str] = []
    depth = 0
    for index, (token_type, value) in enumerate(tokens):
        if is_token_subtype(token_type, Token.Punctuation):
            for char in value:
                if char in "([{":
                    parts.append(
                        _colored(char, BRACKET_COLORS[depth % len(BRACKET_COLORS)])
                    )
                    depth += 1
                elif char in ")]}":
                    depth = max(0, depth - 1)
                    parts.append(
                        _colored(char, BRACKET_COLORS[depth % len(BRACKET_COLORS)])
                    )
                else:
                    parts.append(html.escape(char, quote=False))
            continue
        role = _role(token_type, value, tokens, index)
        parts.append(
            _colored(value, CODE_COLORS[role])
            if role != "text"
            else html.escape(value, quote=False)
        )
    return "".join(parts)


@cache
def _lexer(language: str) -> Lexer | None:
    """Find the lexer for a code fence's language, reused across the many renders of a stream.

    Args:
        language (str): The lowercase language name or alias.

    Returns:
        Lexer | None: The lexer, or None for plain text and unknown languages.
    """
    if not language or language in ("text", "txt", "plaintext", "plain"):
        return None
    try:
        return get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def _role(
    token_type: _TokenType, value: str, tokens: list[tuple[_TokenType, str]], index: int
) -> str:
    """Pick the VS Code colour role for a token.

    Pygments does not tell calls from other names, so plain names are classed the way VS Code's
    semantic colouring would: constants, then types, then calls, then variables.

    Args:
        token_type (_TokenType): The token's Pygments type.
        value (str): The token's text.
        tokens (list[tuple[_TokenType, str]]): Every token in the block.
        index (int): The token's position in the block.

    Returns:
        str: A key of CODE_COLORS.
    """
    if is_token_subtype(token_type, Token.Keyword) and value in CONTROL_KEYWORDS:
        return "control"
    if token_type in (
        Token.Name,
        Token.Name.Other,
        Token.Name.Property,
        Token.Name.Builtin,
    ):
        called = next(
            (
                text.lstrip().startswith("(")
                for _type, text in tokens[index + 1 :]
                if text.strip()
            ),
            False,
        )
        if token_type is Token.Name.Builtin:
            return "function" if called else "type"
        if value.isupper() and len(value) > 1:
            return "constant"
        if value[:1].isupper():
            return "type"
        return "function" if called else "variable"
    while token_type not in TOKEN_ROLES and token_type.parent is not None:
        token_type = token_type.parent
    return TOKEN_ROLES.get(token_type, "text")


def _colored(text: str, rgb: tuple[int, int, int]) -> str:
    """Wrap text in a span of one colour.

    Args:
        text (str): The raw text.
        rgb (tuple[int, int, int]): The colour.

    Returns:
        str: The coloured text as rich text.
    """
    return f"<span style='color: {qss(rgb)};'>{html.escape(text, quote=False)}</span>"
