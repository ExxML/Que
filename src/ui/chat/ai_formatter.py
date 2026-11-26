import html
import re
from functools import cache

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name
from pygments.token import Token, _TokenType, is_token_subtype
from pygments.util import ClassNotFound

from ui.chat.latex import render_math
from ui.chat.source_text import RenderedSources, SourceText
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
BODY_PT = 12.0
CODE_PT = 11.0
BLOCK_GAP = 10  # Pixels between paragraphs, lists, code blocks and tables
ITEM_GAP = 4  # Pixels between list items
HEADING_GAP = 4  # Pixels under a heading, so it sits with the section it introduces
HEADING_STYLES = {  # Level: (point size, pixels above)
    1: (BODY_PT + 3.5, 18),
    2: (BODY_PT + 2.0, 16),
    3: (BODY_PT + 1.0, 14),
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


def format_message(message: str, sources: RenderedSources | None = None) -> str:
    """Convert a Markdown message from any model into rich text for a QLabel.

    Handles headings, paragraphs, nested and task lists, block quotes, tables, rules, fenced code
    with syntax highlighting, and inline bold, italic, strikethrough, code and links. Unclosed
    fences and half-written lines render sensibly so a streaming reply can be shown at every chunk.

    Args:
        message (str): The raw Markdown message text to format.
        sources (RenderedSources | None): Collect source offsets in a side copy of the HTML.

    Returns:
        str: HTML-formatted message string ready for a QLabel.
    """
    lines = _source_lines(message, sources is not None)
    return (
        f"<div style='font-family: {BODY_FONT}; font-size: {BODY_PT}pt; white-space: pre-wrap;'>"
        f"{_render_blocks(lines, BLOCK_GAP, sources=sources)}</div>"
    )


def display_math(line: str) -> re.Match[str] | None:
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


def is_table_start(lines: list[str], i: int) -> bool:
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


def table_cells(row: str) -> list[str]:
    """Split a table row into its trimmed cells, keeping escaped pipes inside cells.

    Args:
        row (str): The table row.

    Returns:
        list[str]: The cell texts.
    """
    return [cell.text for cell in table_cell_sources(SourceText(row))]


def table_cell_sources(row: SourceText) -> list[SourceText]:
    """Split a table row and preserve each cell's original Markdown offsets."""
    row = row.strip()
    if row.text.startswith("|"):
        row = row[1:]
    if row.text.endswith("|") and not row.text.endswith("\\|"):
        row = row[:-1]
    parts = re.split(r"(?<!\\)\|", row.text)
    start = 0
    cells = []
    for part in parts:
        cell = row[start : start + len(part)].strip()
        cells.append(cell.sub_group(re.compile(r"\\(\|)"), 1))
        start += len(part) + 1
    return cells


def _source_lines(message: str, mapped: bool) -> list[SourceText]:
    """Expand tabs and normalize newlines, retaining the original offsets when requested."""
    if not mapped:
        return [
            SourceText(line)
            for line in message.replace("\r\n", "\n").expandtabs(4).split("\n")
        ]
    lines = []
    start = 0
    raw_lines = message.split("\n")
    for index, line in enumerate(raw_lines):
        body = line.removesuffix("\r") if index < len(raw_lines) - 1 else line
        source = SourceText.original(body, start)
        start += len(line) + 1
        if "\t" not in body:
            lines.append(source)
            continue
        column = 0
        parts = []
        for position, char in enumerate(body):
            part = source[position : position + 1]
            if char == "\t":
                part = part.replace_with(" " * (4 - column % 4))
            parts.append(part)
            column = 0 if char == "\r" else column + len(part.text)
        lines.append(SourceText.join(parts))
    return lines


def _render_blocks(
    lines: list[SourceText],
    gap: int,
    depth: int = 0,
    sources: RenderedSources | None = None,
) -> str:
    """Render a run of Markdown lines as a sequence of block elements.

    Args:
        lines (list[SourceText]): The lines to render, stripped of any enclosing indentation.
        gap (int): Pixels between consecutive blocks.
        depth (int): How many lists enclose the lines.
        sources (RenderedSources | None): Collect source offsets for the rendered runs.

    Returns:
        str: The blocks as rich text.
    """
    blocks: list[str] = []
    after_heading = False
    i = 0
    while i < len(lines):
        source = lines[i]
        line = source.text
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
            while i < len(lines) and not closing.match(lines[i].text):
                code.append(_dedent(lines[i], len(indent)).text)
                i += 1
            i += 1  # Skip the closing fence; a stream may not have sent it yet
            blocks.append(
                _code_block("\n".join(_two_space_indent(code)), language, top)
            )
        elif math := display_math(line):
            closer = "$$" if math.group(1) == "$$" else "\\]"
            body = [source[math.start(2) : math.end(2)]]
            i += 1
            while closer not in body[-1].text and i < len(lines):
                body.append(SourceText("\n"))
                body.append(lines[i])
                i += 1
            expression = SourceText.join(body)
            tex = expression.text.split(closer)[0]
            rendered = render_math(tex, display=True)
            if sources is not None:
                rendered = sources.mark(expression[: len(tex)].replace_with(rendered))
            blocks.append(
                f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-top: {top}px;'>"
                f"<tr><td align='center'>{rendered}</td></tr></table>"
            )
        elif heading := HEADING_RE.match(line):
            level = len(heading.group(1))
            size, heading_gap = HEADING_STYLES[level]
            blocks.append(
                f"<p style='margin: {heading_gap if blocks else 0}px 0 0 0; font-size: {size}pt; "
                f"font-weight: bold;'>{_inline(source[heading.start(2) : heading.end(2)], sources)}</p>"
            )
            i += 1
        elif RULE_RE.match(line):
            blocks.append(
                f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-top: {top}px;'>"
                f"<tr><td style='background-color: {RULE}; font-size: 1pt;'>&nbsp;</td></tr></table>"
            )
            i += 1
        elif QUOTE_RE.match(line):
            quoted: list[SourceText] = []
            while i < len(lines) and (quote := QUOTE_RE.match(lines[i].text)):
                quoted.append(lines[i][quote.start(1) : quote.end(1)])
                i += 1
            blocks.append(
                f"<table cellspacing='0' cellpadding='0' style='margin-top: {top}px;'><tr>"
                f"<td width='3' style='background-color: {RULE}; font-size: 1pt;'>&nbsp;</td>"
                f"<td style='padding-left: 10px; color: {MUTED};'>{_render_blocks(quoted, gap, depth, sources)}</td>"
                "</tr></table>"
            )
        elif LIST_RE.match(line):
            items, i = _collect_list(lines, i)
            blocks.append(_list(items, top, depth, sources))
        elif _table_start(lines, i):
            rows, separator = [source], lines[i + 1].text
            i += 2
            while i < len(lines) and "|" in lines[i].text and lines[i].text.strip():
                rows.append(lines[i])
                i += 1
            blocks.append(_table(rows, separator, top, sources))
        else:
            paragraph = [source.strip()]
            i += 1
            while (
                i < len(lines) and lines[i].text.strip() and not _starts_block(lines, i)
            ):
                paragraph.append(lines[i].strip())
                i += 1
            blocks.append(
                f"<p style='margin: {top}px 0 0 0;'>{'<br>'.join(_inline(text, sources) for text in paragraph)}</p>"
            )
    return "".join(blocks)


def _table_start(lines: list[SourceText], index: int) -> bool:
    """Recognize a table using its header and separator."""
    return is_table_start([line.text for line in lines[index : index + 2]], 0)


def _starts_block(lines: list[SourceText], i: int) -> bool:
    """Check whether a line begins a block other than a paragraph.

    Args:
        lines (list[SourceText]): The lines being rendered.
        i (int): The index of the line to check.

    Returns:
        bool: Whether the line interrupts a paragraph.
    """
    line = lines[i].text
    return bool(
        FENCE_RE.match(line)
        or display_math(line)
        or HEADING_RE.match(line)
        or RULE_RE.match(line)
        or QUOTE_RE.match(line)
        or LIST_RE.match(line)
        or _table_start(lines, i)
    )


def _dedent(line: SourceText, width: int) -> SourceText:
    """Remove up to a number of leading spaces from a line.

    Args:
        line (SourceText): The line to dedent.
        width (int): The most spaces to remove.

    Returns:
        SourceText: The dedented line.
    """
    return line[min(width, len(line.text) - len(line.text.lstrip(" "))) :]


def _two_space_indent(lines: list[str]) -> list[str]:
    """Rescale code indentation so each level is two spaces wide.

    The smallest indent in the code is taken as one level; spaces beyond whole levels are kept
    so aligned continuation lines stay aligned.

    Args:
        lines (list[str]): The code lines.

    Returns:
        list[str]: The lines with two-space indent levels.
    """
    widths = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    unit = min((width for width in widths if width), default=0)
    if unit <= 2:
        return lines
    rescaled: list[str] = []
    for line in lines:
        levels, extra = divmod(len(line) - len(line.lstrip(" ")), unit)
        rescaled.append(" " * (levels * 2 + extra) + line.lstrip(" "))
    return rescaled


def _collect_list(
    lines: list[SourceText], start: int
) -> tuple[list[tuple[str, list[SourceText]]], int]:
    """Gather the items of the list that starts at a line, each with its dedented content lines.

    Args:
        lines (list[SourceText]): The lines being rendered.
        start (int): The index of the list's first item.

    Returns:
        tuple[list[tuple[str, list[SourceText]]], int]: Each item's marker and content lines, and the
            index of the first line after the list.
    """
    first = LIST_RE.match(lines[start].text)
    assert first is not None
    indent = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    items: list[tuple[str, list[SourceText]]] = []
    content_indent = 0
    i = start
    while i < len(lines):
        source = lines[i]
        line = source.text
        line_indent = len(line) - len(line.lstrip(" "))
        item = LIST_RE.match(line)
        if item and line_indent < indent + 2:
            if item.group(2)[0].isdigit() != ordered:
                break  # A different kind of list starts here
            items.append((item.group(2), [source[item.start(3) : item.end(3)]]))
            content_indent = line_indent + len(item.group(2)) + 1
        elif not line.strip():
            following = next(
                (part.text for part in lines[i + 1 :] if part.text.strip()), ""
            )
            following_indent = len(following) - len(following.lstrip(" "))
            if not following or (
                following_indent < indent + 2 and not LIST_RE.match(following)
            ):
                break
            items[-1][1].append(SourceText(""))
        elif line_indent >= indent + 2 or (
            items[-1][1][-1].text.strip() and not _starts_block(lines, i)
        ):
            # Indented content belongs to the item; so does an unindented line that continues its text
            items[-1][1].append(_dedent(source, content_indent))
        else:
            break
        i += 1
    return items, i


def _list(
    items: list[tuple[str, list[SourceText]]],
    top: int,
    depth: int,
    sources: RenderedSources | None,
) -> str:
    """Render list items as a table so wrapped lines hang under the text, not the marker.

    Ordered items are numbered on from the first item's number, as Markdown does.

    Args:
        items (list[tuple[str, list[SourceText]]]): Each item's marker and content lines.
        top (int): Pixels above the list.
        depth (int): How many lists enclose this one.
        sources (RenderedSources | None): Collect source offsets for the rendered runs.

    Returns:
        str: The list as rich text.
    """
    ordered = items[0][0][0].isdigit()
    start = int(items[0][0][:-1]) if ordered else 0
    width = 10 + 8 * len(str(start + len(items))) if ordered else 18
    rows: list[str] = []
    for index, (marker, content) in enumerate(items):
        if task := TASK_RE.match(content[0].text):
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
            f"<td valign='top' style='padding-top: {padding}px;'>{_render_blocks(content, ITEM_GAP + 2, depth + 1, sources)}</td></tr>"
        )
    return f"<table cellspacing='0' cellpadding='0' style='margin-top: {top}px;'>{''.join(rows)}</table>"


def _table(
    rows: list[SourceText], separator: str, top: int, sources: RenderedSources | None
) -> str:
    """Render a GitHub-style table with a shaded header row and its column alignments.

    Args:
        rows (list[SourceText]): The header row followed by the body rows.
        separator (str): The separator row, whose colons set each column's alignment.
        top (int): Pixels above the table.
        sources (RenderedSources | None): Collect source offsets for the rendered runs.

    Returns:
        str: The table as rich text.
    """
    alignments = [
        "center"
        if cell.startswith(":") and cell.endswith(":")
        else "right"
        if cell.endswith(":")
        else "left"
        for cell in table_cells(separator)
    ]
    html_rows: list[str] = []
    for index, row in enumerate(rows):
        cells: list[str] = []
        for column, cell in enumerate(table_cell_sources(row)):
            align = alignments[column] if column < len(alignments) else "left"
            style = f"padding: 4px 10px; border: 1px solid {RULE};"
            if index == 0:
                style += f" background-color: {CODE_BG}; font-weight: 600;"
            cells.append(
                f"<td align='{align}' valign='top' style='{style}'>{_inline(cell, sources)}</td>"
            )
        html_rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f"<table cellspacing='0' cellpadding='0' style='margin-top: {top}px; border-collapse: collapse;'>"
        f"{''.join(html_rows)}</table>"
    )


def _inline(text: SourceText, sources: RenderedSources | None) -> str:
    """Render inline Markdown (code, maths, links, emphasis) in one line of text.

    Args:
        text (SourceText): The raw text.
        sources (RenderedSources | None): Collect source offsets for the rendered runs.

    Returns:
        str: The text as rich text.
    """
    protected: list[SourceText] = []

    def protect(fragment: SourceText) -> SourceText:
        """Hide a finished fragment from later rules behind a placeholder.

        Args:
            fragment (SourceText): The rich text to hide.

        Returns:
            SourceText: The placeholder.
        """
        protected.append(fragment)
        return SourceText(f"\x00{len(protected) - 1}\x00")

    text = text.sub(
        CODE_SPAN_RE,
        lambda match: protect(
            text[match.start() : match.end()].replace_with(
                f"<span style='font-family: {CODE_FONT}; font-size: {CODE_PT}pt; color: {qss(CODE_COLORS['text'])}; "
                f"background-color: {CODE_BG};'>{html.escape(match.group(2).strip(), quote=False)}</span>"
            )
        ),
    )
    text = text.sub(
        INLINE_MATH_RE,
        lambda match: protect(
            text[match.start() : match.end()].replace_with(
                render_math(
                    next(tex for tex in match.groups() if tex is not None),
                    display=False,
                )
            )
        ),
    )
    text = text.sub(
        ESCAPED_RE,
        lambda match: protect(text[match.start(1) : match.end(1)].escape_html()),
    )
    text = text.escape_html()
    text = text.sub(LINE_BREAK_TAG_RE, lambda _match: protect(SourceText("<br>")))
    # Links show only their coloured text: the overlay never opens them or changes the cursor over them
    text = text.sub(
        LINK_RE,
        lambda match: SourceText.join(
            (
                protect(SourceText(LINK_OPEN)),
                text[match.start(1) : match.end(1)],
                SourceText("</span>"),
            )
        ),
    )
    for pattern, replacement in EMPHASIS_RULES:
        before, after = replacement.split(r"\1")
        text = text.sub_group(pattern, 1, before, after)
    text = text.sub(PLACEHOLDER_RE, lambda match: protected[int(match.group(1))])
    return sources.mark(text) if sources is not None else text.text


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
        f"<tr><td style='padding: 6px 12px 0 12px; font-size: {CODE_PT - 1}pt; color: {MUTED};'>"
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
