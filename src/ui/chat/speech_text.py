"""Turn a Markdown reply into the sentences Que speaks, worded the way a person would say them.

Code blocks are never spoken. Inline code, symbols and maths are read as words, so `<=` is
"less than or equal to" and `fs.getFileSystem()` is "fs get file system". Every sentence keeps
the offsets of the Markdown it came from, which only grows while a reply streams.
"""

import re
from typing import NamedTuple

from ui.chat.ai_formatter import (
    CODE_SPAN_RE,
    EMPHASIS_RULES,
    ESCAPED_RE,
    FENCE_RE,
    HEADING_RE,
    INLINE_MATH_RE,
    LINK_RE,
    LIST_RE,
    QUOTE_RE,
    RULE_RE,
    TABLE_SEPARATOR_RE,
    TASK_RE,
    display_math,
    is_table_start,
    table_cell_sources,
)
from ui.chat.latex import SYMBOLS, TOKEN_RE
from ui.chat.source_text import SourceSpan, SourceText

SENTENCE_PAUSE = 0.25  # Seconds of silence after a sentence
BLOCK_PAUSE = (
    0.45  # Seconds of silence after a heading, list item, table row or paragraph
)

IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\([^)\s]+(?:\s+\"[^)]*\")?\)")
URL_RE = re.compile(r"<?\bhttps?://[^\s<>]+>?|\bwww\.[^\s<>]+")
TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
PLURAL_RE = re.compile(r"(?<=[A-Za-z])\((e?s)\)")
# A name followed by brackets, with one level of nesting: f(x), O(n log n), arr[i], List<int>
CALL_RE = re.compile(
    r"(?<![\w.])[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*"
    r"(?:\((?:[^()\n]|\([^()\n]*\))*\)|\[[^\[\]\n]*\]|<[\w\s,<>.?]*>)+"
)
PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")
WORD_RE = re.compile(r"[A-Za-z0-9]+")
SENTENCE_END_RE = re.compile(r"[.!?]+[\"')\]]*(?=\s)")
TERMINAL_RE = re.compile(r"[.!?:;,][\"')\]]*$")
EMOJI_RE = re.compile("[\U0001f000-\U0001faff☀-⛿✀-➿⬀-⯿️‍⃣]")
STRAY_MARKS_RE = re.compile(r"(?<![\w*])\*+|\*+(?![\w*])|(?<!\w)_+|_+(?!\w)|~~|`+|\\")
SPACES_RE = re.compile(r"\s+")
SPOKEN_WORD_RE = re.compile(r"\S+")
SPOKEN_EDGES = "\"'“”‘’()[].,;:!?"
NUMBER_UNIT_RE = re.compile(
    r"^([~≈]?)(\d[\d,]*(?:\.\d+)?)(ms|µs|us|ns|kb|mb|gb|tb|khz|mhz|ghz|hz|fps|x|k)$",
    re.IGNORECASE,
)
RANGE_RE = re.compile(r"^(\d[\d,.]*)[-–](\d[\d,.]*)$")
PATH_RE = re.compile(r"^(?:[A-Za-z]:)?(?:~|\.{1,2})?[\\/]?(?:[\w.@-]+[\\/])+[\w.@-]*$")

# fmt: off
ABBREVIATIONS = {
    "e.g.": "for example", "eg.": "for example", "i.e.": "that is", "ie.": "that is",
    "etc.": "et cetera", "vs.": "versus", "vs": "versus", "approx.": "approximately",
    "w/": "with", "w/o": "without", "&": "and", "&&": "and", "c#": "C sharp", "f#": "F sharp",
    "c++": "C plus plus", ".net": "dot net",
}
NO_SPLIT = frozenset(
    ["e.g.", "i.e.", "eg.", "ie.", "etc.", "vs.", "approx.", "mr.", "mrs.", "ms.", "dr.",
     "st.", "jr.", "sr.", "fig.", "no.", "vol.", "inc.", "ltd.", "co."]
)
UNITS = {
    "ms": "milliseconds", "µs": "microseconds", "us": "microseconds", "ns": "nanoseconds",
    "kb": "kilobytes", "mb": "megabytes", "gb": "gigabytes", "tb": "terabytes", "hz": "hertz",
    "khz": "kilohertz", "mhz": "megahertz", "ghz": "gigahertz", "fps": "frames per second",
    "x": "times", "k": "thousand",
}
# Operators written in prose, standing on their own between spaces
PROSE_OPERATORS = {
    "<=": "less than or equal to", ">=": "greater than or equal to", "==": "equals",
    "===": "equals", "!=": "is not equal to", "!==": "is not equal to", "=": "equals",
    "<": "less than", ">": "greater than", "+": "plus", "*": "times", "/": "divided by",
    "->": "to", "=>": "gives", "<-": "from", "<->": "to and from", "-": ",", "--": ",",
    "|": ",", "||": "or", "~": "about", "%": "percent", "^": "to the power of",
}
SYMBOL_WORDS = {
    "≤": " less than or equal to ", "≥": " greater than or equal to ", "≠": " not equal to ",
    "≈": " approximately ", "×": " times ", "÷": " divided by ", "±": " plus or minus ",
    "→": " to ", "⟶": " to ", "←": " from ", "↔": " to and from ", "⇒": " implies ",
    "⟹": " implies ", "⇔": " if and only if ", "∞": " infinity ", "√": " square root of ",
    "∑": " the sum of ", "∏": " the product of ", "∫": " the integral of ", "∈": " in ",
    "∉": " not in ", "∀": " for all ", "∃": " there exists ", "∅": " the empty set ",
    "∪": " union ", "∩": " intersect ", "⊆": " subset of ", "⊂": " subset of ", "°": " degrees ",
    "…": "... ", "—": ", ", "–": ", ", "•": ", ", "·": ", ", "‘": "", "’": "'", "“": "", "”": "",
    "²": " squared ", "³": " cubed ", "½": " one half ", "¼": " one quarter ", "¾": " three quarters ",
    "∧": " and ", "∨": " or ", "¬": " not ", "⊕": " xor ", "∆": " delta ", "Δ": " delta ",
    "λ": " lambda ", "π": " pi ", "θ": " theta ", "μ": " mu ", "σ": " sigma ", "α": " alpha ",
    "β": " beta ", "ε": " epsilon ", "Θ": " theta ", "Ω": " omega ", "∂": " partial ",
}
CODE_OPERATORS = {
    "===": "strictly equals", "!==": "is not strictly equal to", "**=": "power equals",
    "<<=": "shift left equals", ">>=": "shift right equals", ">>>": "unsigned shift right",
    "<=>": "compared with", "...": "", "==": "equals", "!=": "is not equal to",
    "<=": "less than or equal to", ">=": "greater than or equal to", "&&": "and", "||": "or",
    "->": "arrow", "=>": "arrow", "::": "", "++": "plus plus", "--": "minus minus",
    "+=": "plus equals", "-=": "minus equals", "*=": "times equals", "/=": "divided by equals",
    "%=": "mod equals", "&=": "and equals", "|=": "or equals", "^=": "xor equals",
    "//": "floor divided by", "**": "to the power of", "<<": "shift left", ">>": "shift right",
    ":=": "is assigned", "??": "or else", "?.": "", "..": "to", "=": "equals", "<": "less than",
    ">": "greater than", "+": "plus", "-": "minus", "*": "times", "/": "divided by", "%": "mod",
    "^": "xor", "&": "and", "|": "or", "!": "not", "~": "not", "?": "", ":": ",", ";": ",",
    ",": ",", ".": "", "@": "at", "#": "", "\\": "", "`": "", "$": "",
}
UNARY_OPERATORS = {"-": "minus", "+": "", "*": "", "**": "", "&": "", "!": "not", "~": "not", "...": ""}
ALIASES = {
    "len": "length", "idx": "index", "arr": "array", "ptr": "pointer", "str": "string",
    "std": "standard", "num": "number", "nums": "numbers", "cnt": "count", "tmp": "temp",
    "prev": "previous", "curr": "current", "cur": "current", "dst": "destination",
    "src": "source", "fn": "function", "func": "function", "args": "arguments",
    "kwargs": "keyword arguments", "params": "parameters", "impl": "implementation",
    "obj": "object", "msg": "message", "val": "value", "vals": "values", "elif": "else if",
    "lhs": "left hand side", "rhs": "right hand side", "eq": "equals", "nil": "nil",
    "fs": "fs", "os": "OS", "io": "IO", "db": "DB", "ui": "UI", "id": "ID", "ids": "IDs",
}
EXTENSIONS = {
    "py": "pie", "js": "JS", "ts": "TS", "jsx": "JSX", "tsx": "TSX", "mjs": "MJS",
    "json": "jason", "md": "MD", "txt": "text", "html": "HTML", "htm": "HTM", "css": "CSS",
    "scss": "SCSS", "c": "C", "h": "H", "cc": "CC", "cpp": "CPP", "hpp": "HPP", "cs": "CS",
    "java": "java", "kt": "KT", "go": "go", "rs": "RS", "rb": "RB", "php": "PHP",
    "swift": "swift", "sh": "SH", "ps1": "PS1", "bat": "bat", "yml": "yaml", "yaml": "yaml",
    "toml": "toml", "ini": "INI", "cfg": "config", "xml": "XML", "csv": "CSV", "sql": "SQL",
    "exe": "EXE", "dll": "DLL", "so": "SO", "lock": "lock", "log": "log", "env": "env",
    "png": "PNG", "jpg": "JPEG", "svg": "SVG", "pdf": "PDF", "zip": "zip", "com": "com",
    "org": "org", "net": "net", "io": "IO", "ai": "AI", "dev": "dev", "app": "app",
}
TEX_WORDS = {
    "cdot": "times", "times": "times", "ast": "times", "div": "divided by", "pm": "plus or minus",
    "mp": "minus or plus", "le": "less than or equal to", "leq": "less than or equal to",
    "leqslant": "less than or equal to", "ge": "greater than or equal to",
    "geq": "greater than or equal to", "geqslant": "greater than or equal to",
    "ne": "is not equal to", "neq": "is not equal to", "approx": "approximately",
    "sim": "is similar to", "equiv": "is equivalent to", "propto": "is proportional to",
    "ll": "much less than", "gg": "much greater than", "to": "to", "rightarrow": "to",
    "longrightarrow": "to", "mapsto": "maps to", "leftarrow": "from", "gets": "gets",
    "Rightarrow": "implies", "implies": "implies", "Longrightarrow": "implies",
    "iff": "if and only if", "Leftrightarrow": "if and only if", "infty": "infinity",
    "in": "in", "notin": "not in", "subset": "is a subset of", "subseteq": "is a subset of",
    "supset": "is a superset of", "supseteq": "is a superset of", "cup": "union",
    "cap": "intersect", "setminus": "minus", "forall": "for all", "exists": "there exists",
    "nexists": "there is no", "emptyset": "the empty set", "varnothing": "the empty set",
    "neg": "not", "lnot": "not", "land": "and", "wedge": "and", "lor": "or", "vee": "or",
    "oplus": "xor", "otimes": "tensor", "circ": "composed with", "partial": "partial",
    "nabla": "del", "ldots": "and so on", "cdots": "and so on", "dots": "and so on",
    "dotsc": "and so on", "dotsb": "and so on", "lfloor": "the floor of", "rfloor": "",
    "lceil": "the ceiling of", "rceil": "", "langle": "", "rangle": "", "mid": "divides",
    "mod": "mod", "bmod": "mod", "prime": "prime", "angle": "angle", "degree": "degrees",
    "therefore": "therefore", "because": "because", "hbar": "h bar", "ell": "L",
    "sin": "sine", "cos": "cosine", "tan": "tangent", "sec": "secant", "csc": "cosecant",
    "cot": "cotangent", "arcsin": "arc sine", "arccos": "arc cosine", "arctan": "arc tangent",
    "sinh": "hyperbolic sine", "cosh": "hyperbolic cosine", "tanh": "hyperbolic tangent",
    "log": "log", "lg": "log", "ln": "natural log", "exp": "exp", "max": "max", "min": "min",
    "gcd": "G C D", "det": "the determinant of", "deg": "degree", "dim": "dimension",
    "sup": "sup", "inf": "inf", "arg": "arg", "Pr": "the probability of", "vert": "",
    "lvert": "", "rvert": "", "Vert": "", "lVert": "", "rVert": "", "backslash": "",
    "lbrace": "", "rbrace": "", "colon": ",", "perp": "is perpendicular to", "parallel": "is parallel to",
    "cong": "is congruent to", "simeq": "is approximately", "star": "star", "bullet": "",
    "dagger": "dagger", "aleph": "aleph", "Re": "the real part of", "Im": "the imaginary part of",
}
TEX_BIG = {"sum": "the sum", "prod": "the product", "int": "the integral", "iint": "the double integral",
           "oint": "the contour integral", "lim": "the limit", "bigcup": "the union", "bigcap": "the intersection"}
TEX_TEXT = frozenset(
    ["text", "textrm", "textnormal", "mbox", "mathrm", "operatorname", "operatorname*", "textsf",
     "mathsf", "textup", "textbf", "textit", "emph", "mathit", "texttt", "mathtt", "mathbf",
     "boldsymbol", "mathbb", "mathcal", "mathscr", "mathfrak", "bm"]
)
TEX_SKIP = frozenset(
    ["left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr",
     "biggl", "biggr", "Biggl", "Biggr", "bigm", "Bigm", "displaystyle", "textstyle",
     "scriptstyle", "limits", "nolimits", "nonumber", "notag", "begin", "end", "hat",
     "widehat", "tilde", "widetilde", "vec", "dot", "ddot", "bar", "overline", "underline",
     "check", "breve", "acute", "grave", "quad", "qquad"]
)
TEX_CHARS = {
    "=": "equals", "<": "less than", ">": "greater than", "+": "plus", "-": "minus", "*": "times",
    "/": "over", "!": "factorial", ",": ",", ";": ",", "'": "prime", "&": ",", "|": "",
    "(": "", ")": "", "[": "", "]": "", ".": "", ":": "", "~": "",
}
# fmt: on
GREEK = frozenset(name for name, char in SYMBOLS.items() if "Ͱ" <= char <= "Ͽ")
OPERATOR_PATTERN = "|".join(
    re.escape(op) for op in sorted(CODE_OPERATORS, key=len, reverse=True)
)
CODE_TOKEN_RE = re.compile(
    r"(?P<number>0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"|(?P<string>\"[^\"]*\"|'[^']*')"
    r"|(?P<open>[(\[{])|(?P<close>[)\]}])"
    rf"|(?P<op>{OPERATOR_PATTERN})"
    r"|(?P<space>\s+)"
    r"|(?P<other>.)"
)
IDENTIFIER_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class SpeechWord(NamedTuple):
    """Identify a spoken word and the range in the original Markdown it reads."""

    text: str
    source: SourceSpan


class SpeechUnit(NamedTuple):
    """One stretch of speech and the Markdown it reads.

    Attributes:
        text (str): The words to speak.
        start (int): The offset of its first Markdown character.
        end (int): The offset just past its last Markdown character.
        pause (float): Seconds of silence to leave after it.
        open (bool): Whether more streamed text may still extend it.
        words (tuple[SpeechWord, ...]): The spoken words associated with Markdown ranges.
    """

    text: str
    start: int
    end: int
    pause: float
    open: bool
    words: tuple[SpeechWord, ...] = ()


def speech_units(
    markdown: str, complete: bool, start: int = 0, *, first_only: bool = False
) -> list[SpeechUnit]:
    """Split a reply into speakable sentences from an offset onward, skipping code blocks.

    A sentence that straddles the offset is spoken from the offset. While the reply is still
    streaming, only text that later chunks cannot change is included, and a final sentence that
    may still grow is marked open.

    Args:
        markdown (str): The reply's Markdown, possibly half streamed.
        complete (bool): Whether the reply has finished.
        start (int): The Markdown offset to speak from.
        first_only (bool): Stop once the next speakable unit is known.

    Returns:
        list[SpeechUnit]: The sentences to speak, in order, none of them empty.
    """
    limit = len(markdown) if complete else stable_end(markdown)
    text = markdown[:limit]
    ended = complete or text.endswith("\n")
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in text.split("\n"):
        lines.append((offset, line.rstrip("\r")))
        offset += len(line) + 1
    texts = [line for _offset, line in lines]
    pieces: list[tuple[int, int, str]] = []  # Markdown start, end and kind of each line
    i = 0
    while i < len(lines):
        line_start, line = lines[i]
        if not line.strip():
            i += 1
        elif fence := FENCE_RE.match(line):
            marker = fence.group(2)
            closing = re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")
            i += 1
            while i < len(lines) and not closing.match(lines[i][1]):
                i += 1
            i += (
                1  # An unclosed fence hides the rest of the reply, as it does on screen
            )
        elif math := display_math(line):
            closer = "$$" if math.group(1) == "$$" else "\\]"
            tail = math.group(2)
            i += 1
            while closer not in tail and i < len(lines):
                tail = lines[i][1]
                i += 1
            if closer not in tail and not complete:
                break  # Read the formula once the stream has sent all of it
            last_start, last_line = lines[i - 1]
            pieces.append(
                (line_start + math.start(2), last_start + len(last_line), "math")
            )
        elif heading := HEADING_RE.match(line):
            pieces.append(
                (line_start + heading.start(2), line_start + heading.end(2), "block")
            )
            i += 1
        elif RULE_RE.match(line):
            i += 1
        elif is_table_start(texts, i):
            pieces.append((line_start, line_start + len(line), "row"))
            i += 2
            while i < len(lines) and "|" in lines[i][1] and lines[i][1].strip():
                pieces.append((lines[i][0], lines[i][0] + len(lines[i][1]), "row"))
                i += 1
        else:
            content = 0
            rest = line
            while quote := QUOTE_RE.match(rest):
                content += quote.start(1)
                rest = quote.group(1)
            if item := LIST_RE.match(rest):
                content += item.start(3)
                rest = item.group(3)
                if task := TASK_RE.match(rest):
                    content += task.end()
            following = lines[i + 1][1] if i + 1 < len(lines) else ""
            # A line followed by more of its paragraph reads on with a sentence pause
            kind = (
                "line"
                if following.strip()
                and not LIST_RE.match(following)
                and not HEADING_RE.match(following)
                else "block"
            )
            pieces.append((line_start + content, line_start + len(line), kind))
            i += 1
    units: list[SpeechUnit] = []
    for index, (piece_start, piece_end, kind) in enumerate(pieces):
        if piece_end <= start:
            continue
        last_piece = index == len(pieces) - 1
        growing = last_piece and not ended and piece_end == limit
        pause = SENTENCE_PAUSE if kind == "line" else BLOCK_PAUSE
        if kind == "row" and growing:
            break  # A row is read whole, once its line is finished
        if kind == "row":
            row = SourceText.original(text[piece_start:piece_end], piece_start)
            fragments = [
                spoken_cell
                for cell in table_cell_sources(row)
                if (spoken_cell := _speak_inline_text(cell)).text
            ]
            spoken = SourceText.join(
                part
                for index, fragment in enumerate(fragments)
                for part in (SourceText(", " if index else ""), fragment)
            )
            if spoken.text:
                units.append(_speech_unit(spoken, piece_start, piece_end, pause, False))
                if first_only:
                    return units
            continue
        if kind == "math":
            tex = text[piece_start:piece_end]
            tex = tex.split("$$")[0].split("\\]")[0]
            spoken_math = speak_tex(tex)
            if spoken_math:
                expression = SourceText.original(tex, piece_start).replace_with(
                    spoken_math
                )
                units.append(
                    _speech_unit(expression, piece_start, piece_end, pause, False)
                )
                if first_only:
                    return units
            continue
        sentences = _sentences(text, piece_start, piece_end)
        for number, (sentence_start, sentence_end) in enumerate(sentences):
            if sentence_end <= start:
                continue
            final = number == len(sentences) - 1
            begin = max(
                sentence_start,
                _protected_start(text, sentence_start, sentence_end, start),
            )
            spoken = _speak_inline_text(
                SourceText.original(text[begin:sentence_end], begin)
            )
            if not spoken.text:
                continue
            is_open = (
                growing
                and final
                and not TERMINAL_RE.search(text[sentence_start:sentence_end])
            )
            units.append(
                _speech_unit(
                    spoken,
                    begin,
                    sentence_end,
                    pause if final else SENTENCE_PAUSE,
                    is_open,
                )
            )
            if first_only:
                return units
    return units


def stable_end(markdown: str) -> int:
    """Find how much of a streaming reply later chunks can no longer change.

    The stable text ends at the last whitespace, so no word is cut in half, and before any
    code span, maths, link or table row on the last line that is still waiting for the rest of
    its markup.

    Args:
        markdown (str): The streamed Markdown so far.

    Returns:
        int: The offset where the stable text ends.
    """
    end = max(markdown.rfind(char) for char in " \t\n") + 1
    line_start = markdown.rfind("\n", 0, end) + 1 if end else 0
    # The line being written ends at the stable end; the one before it may still gain a separator row
    partial = markdown[line_start:end]
    stripped = partial.lstrip(" >")
    if stripped.startswith(("`", "~", "$$", "\\[", "|")) or (
        stripped and set(stripped.strip()) <= set("|-: ")
    ):
        end = line_start
    elif cut := _unfinished_markup(partial):
        end = line_start + cut
    written = markdown[line_start:].strip()
    if end == line_start and line_start and set(written) <= set("|-: "):
        # The new line may still become a separator row that makes the line above a table header
        previous_start = markdown.rfind("\n", 0, line_start - 1) + 1
        previous = markdown[previous_start : line_start - 1]
        before_start = markdown.rfind("\n", 0, max(previous_start - 1, 0)) + 1
        before = (
            markdown[before_start : max(previous_start - 1, 0)]
            if previous_start
            else ""
        )
        if (
            "|" in previous
            and "|" not in before
            and not TABLE_SEPARATOR_RE.match(previous)
        ):
            end = previous_start
    return end


def source_offset(markdown: str, rendered: str, rendered_offset: int) -> int:
    """Find where in the Markdown a line of the rendered reply starts.

    The first words of the rendered line, counted among the same word runs earlier in the
    rendered text, are found at the same occurrence in the Markdown. The rendered text drops
    markup but keeps the words in order, so this needs nothing from the formatter.

    Args:
        markdown (str): The reply's Markdown.
        rendered (str): The plain text of the rendered reply.
        rendered_offset (int): The offset where the clicked line starts in the rendered text.

    Returns:
        int: The Markdown offset to speak from.
    """
    source_words = list(WORD_RE.finditer(markdown))
    source_tokens = [match.group().lower() for match in source_words]
    before = [token.lower() for token in WORD_RE.findall(rendered[:rendered_offset])]
    line = [token.lower() for token in WORD_RE.findall(rendered[rendered_offset:])]
    for length in range(min(5, len(line)), 0, -1):
        run = line[:length]
        seen = sum(
            1
            for index in range(len(before) - length + 1)
            if before[index : index + length] == run
        )
        found = [
            index
            for index in range(len(source_tokens) - length + 1)
            if source_tokens[index : index + length] == run
        ]
        if len(found) > seen:
            return source_words[found[seen]].start()
        if found and length == 1:
            return source_words[found[-1]].start()
    if not rendered:
        return 0
    return round(len(markdown) * rendered_offset / len(rendered))


def speak_code(code: str) -> str:
    """Read a piece of code the way a programmer would say it aloud.

    Args:
        code (str): The code, such as the inside of a code span.

    Returns:
        str: The words to speak.
    """
    code = code.strip()
    if PATH_RE.match(code) and re.search(r"[\\/]", code) and not re.search(r"\s", code):
        last = re.split(r"[\\/]", code.rstrip("\\/"))[-1]
        return speak_code(last) if last and last != code else ""
    tokens = [
        (match.lastgroup or "other", match.group())
        for match in CODE_TOKEN_RE.finditer(code)
    ]
    words: list[str] = []
    previous = ""  # Kind of the last token that was not whitespace
    generics = 0
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        following = next(((k, v) for k, v in tokens[i + 1 :] if k != "space"), ("", ""))
        spaced = i + 1 < len(tokens) and tokens[i + 1][0] == "space"
        if kind == "space":
            i += 1
            continue
        if kind == "number":
            words.append(_speak_number(value))
        elif kind == "name":
            words.append(_split_identifier(value))
        elif kind == "string":
            words.append(_speak_inline(value[1:-1]))
        elif kind == "open":
            if following[1] == {"(": ")", "[": "]", "{": "}"}[value]:
                i = tokens.index(following, i + 1)  # Empty brackets say nothing
            elif value in "([" and previous in ("name", "close"):
                words.append("of" if value == "(" else "at")
        elif kind == "op":
            unary = previous in ("", "op", "open") or (previous == "comma")
            if (
                value == "."
                and previous in ("name", "close")
                and following[0] == "name"
            ):
                extension = following[1].lower()
                rest = tokens[tokens.index(following, i + 1) + 1 :]
                if extension in EXTENSIONS and not any(
                    k in ("name", "open") for k, _v in rest
                ):
                    words.append(f"dot {EXTENSIONS[extension]}")
                    i = tokens.index(following, i + 1)
            elif value == "." and following[0] == "name":
                words.append("dot")
            elif (
                value == "<"
                and previous == "name"
                and not spaced
                and ">" in code[code.find("<") :]
            ):
                generics += 1
                words.append("of")
            elif value == ">" and generics:
                generics -= 1
            elif (
                value == "^"
                and not spaced
                and following[0] in ("number", "name")
                or value == "**"
                and not unary
            ):
                if following[1] in ("2", "3"):
                    words.append("squared" if following[1] == "2" else "cubed")
                    i = tokens.index(following, i + 1)
                else:
                    words.append("to the power of")
            elif unary and value in UNARY_OPERATORS:
                words.append(UNARY_OPERATORS[value])
            elif value in (",", ":", ";"):
                if words:
                    words[-1] += ","
            else:
                words.append(CODE_OPERATORS[value])
            previous = "comma" if value in (",", ":", ";") else "op"
            i += 1
            continue
        elif kind == "other":
            words.append(SYMBOL_WORDS.get(value, "").strip())
        previous = kind if kind != "string" else "name"
        i += 1
    return _tidy(" ".join(word for word in words if word))


def speak_tex(tex: str) -> str:
    """Read LaTeX maths as words, such as "x squared over 2".

    Args:
        tex (str): The LaTeX between the maths delimiters.

    Returns:
        str: The words to speak.
    """
    reader = _TexReader(tex)
    return _tidy(reader.read(""))


class _TexReader:
    """Read a LaTeX token stream into words, one group at a time."""

    def __init__(self, tex: str) -> None:
        self.tokens = [token for token in TOKEN_RE.findall(tex) if not token.isspace()]
        self.position = 0

    def read(self, until: str) -> str:
        """Read tokens until a closing token or the end.

        Args:
            until (str): The token that ends this group, or an empty string for the end.

        Returns:
            str: The group's words.
        """
        words: list[str] = []
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            self.position += 1
            if token == until:
                break
            words.append(self._token(token))
        return " ".join(word for word in words if word)

    def _argument(self) -> str:
        """Read the next braced group or single token.

        Returns:
            str: The argument's words.
        """
        if self.position >= len(self.tokens):
            return ""
        token = self.tokens[self.position]
        self.position += 1
        if token == "{":
            return self.read("}")
        return self._token(token)

    def _peek(self, token: str) -> bool:
        """Consume the next token when it is the given one.

        Args:
            token (str): The token to look for.

        Returns:
            bool: Whether it was there.
        """
        if self.position < len(self.tokens) and self.tokens[self.position] == token:
            self.position += 1
            return True
        return False

    def _token(self, token: str) -> str:
        """Read one token, with any arguments it takes.

        Args:
            token (str): The token.

        Returns:
            str: Its words.
        """
        if token == "{":
            return self.read("}")
        if token in ("}", "$"):
            return ""
        if token == "^":
            exponent = self._argument()
            if exponent in ("2", "3"):
                return "squared" if exponent == "2" else "cubed"
            return "transpose" if exponent == "T" else f"to the power of {exponent}"
        if token == "_":
            return f"sub {self._argument()}"
        if token.isdigit() or token == ".":
            # Gather a whole number, including its decimal point
            number = token
            while self.position < len(self.tokens) and (
                self.tokens[self.position].isdigit()
                or (self.tokens[self.position] == "." and number[-1:].isdigit())
            ):
                number += self.tokens[self.position]
                self.position += 1
            return number if number != "." else ""
        if not token.startswith("\\"):
            if token.isalpha():
                return token.upper() if len(token) == 1 else token
            return TEX_CHARS.get(token, SYMBOL_WORDS.get(token, "").strip())
        name = token[1:]
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            numerator = self._argument()
            return f"{numerator} over {self._argument()}"
        if name in ("binom", "dbinom", "tbinom"):
            top = self._argument()
            return f"{top} choose {self._argument()}"
        if name == "sqrt":
            if self._peek("["):
                root = self.read("]")
                return f"the {root}th root of {self._argument()}"
            return f"the square root of {self._argument()}"
        if name in TEX_TEXT:
            argument_start = self.position
            if self._peek("{"):
                depth = 1
                while self.position < len(self.tokens) and depth:
                    depth += {"{": 1, "}": -1}.get(self.tokens[self.position], 0)
                    self.position += 1
                raw = "".join(self.tokens[argument_start + 1 : self.position - 1])
                return (
                    "big O" if name == "mathcal" and raw == "O" else _speak_inline(raw)
                )
            return self._argument()
        if name in ("log", "lg") and self._peek("_"):
            return f"log base {self._argument()} of"
        if name in TEX_BIG:
            lower = self._argument() if self._peek("_") else ""
            upper = self._argument() if self._peek("^") else ""
            if name == "lim":
                return f"the limit as {lower} of" if lower else "the limit of"
            limits = f" from {lower}" if lower else ""
            limits += f" to {upper}" if upper else ""
            return f"{TEX_BIG[name]}{limits} of"
        if name in ("pmod",):
            return f"mod {self._argument()}"
        if name in TEX_SKIP:
            if name in ("begin", "end"):
                self._argument()
            return ""
        if name in TEX_WORDS:
            return TEX_WORDS[name]
        if name in GREEK:
            return name.removeprefix("var")
        if name in (
            ",",
            ";",
            ":",
            "!",
            " ",
            "\\",
            "{",
            "}",
            "|",
            "%",
            "$",
            "#",
            "&",
            "_",
        ):
            return "," if name == "\\" else ""
        return name


def _sentences(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split a line of Markdown into sentences, never inside code, maths or a link.

    Args:
        text (str): The Markdown.
        start (int): The line's content start.
        end (int): The line's end.

    Returns:
        list[tuple[int, int]]: Each sentence's start and end offsets.
    """
    line = text[start:end]
    protected = [
        match.span()
        for pattern in (CODE_SPAN_RE, INLINE_MATH_RE, LINK_RE, URL_RE)
        for match in pattern.finditer(line)
    ]
    sentences: list[tuple[int, int]] = []
    begin = 0
    for match in SENTENCE_END_RE.finditer(line + " "):
        cut = match.end()
        if cut > len(line) or any(left < cut <= right for left, right in protected):
            continue
        word = line[:cut].split()[-1].lower() if line[:cut].split() else ""
        word = word.lstrip("(\"'")
        if word in NO_SPLIT or re.fullmatch(r"(?:[a-z]\.)+", word):
            continue
        if line[begin:cut].strip():
            sentences.append((begin, cut))
        begin = cut
    if line[begin:].strip():
        sentences.append((begin, len(line)))
    trimmed: list[tuple[int, int]] = []
    for left, right in sentences:
        segment = line[left:right]
        left += len(segment) - len(segment.lstrip())
        right -= len(segment) - len(segment.rstrip())
        trimmed.append((start + left, start + right))
    return trimmed


def _protected_start(text: str, start: int, end: int, offset: int) -> int:
    """Move a start offset back to the start of any code span, maths or link it falls inside.

    Args:
        text (str): The Markdown.
        start (int): The sentence's start.
        end (int): The sentence's end.
        offset (int): The offset to speak from.

    Returns:
        int: The offset to speak from, outside any markup.
    """
    if offset <= start:
        return start
    sentence = text[start:end]
    for pattern in (CODE_SPAN_RE, INLINE_MATH_RE, LINK_RE, IMAGE_RE, URL_RE):
        for match in pattern.finditer(sentence):
            if start + match.start() < offset < start + match.end():
                offset = start + match.start()
    return offset


def _unfinished_markup(line: str) -> int:
    """Find where a half-written code span, maths or link starts on the line being streamed.

    Args:
        line (str): The stable part of the last line.

    Returns:
        int: The offset of the unfinished markup within the line, or zero for none.
    """
    cut = len(line)
    spans = [match.span() for match in CODE_SPAN_RE.finditer(line)]
    for index, char in enumerate(line):
        if char == "`" and not any(left <= index < right for left, right in spans):
            cut = min(cut, index)
            break
    outside = CODE_SPAN_RE.sub(lambda match: " " * len(match.group()), line)
    maths = [match.span() for match in INLINE_MATH_RE.finditer(outside)]
    for match in re.finditer(r"\\\(|\$(?=[^\s$\d])", outside):
        if not any(left <= match.start() < right for left, right in maths):
            cut = min(cut, match.start())
            break
    links = [match.span() for match in LINK_RE.finditer(outside)]
    for match in re.finditer(r"\[", outside):
        closed = outside.find("]", match.start())
        waiting = (
            closed < 0
            or outside[closed + 1 : closed + 2] == "("
            and ")" not in outside[closed:]
        )
        if waiting and not any(left <= match.start() < right for left, right in links):
            cut = min(cut, match.start())
            break
    return cut if cut < len(line) else 0


def _speak_inline(text: str) -> str:
    """Read a line of Markdown prose as words."""
    return _speak_inline_text(SourceText(text)).text


def _speak_inline_text(text: SourceText) -> SourceText:
    """Read Markdown prose and carry source spans through pronunciation changes."""
    kept: list[SourceText] = []

    def keep(match: re.Match[str], words: str) -> SourceText:
        """Hide a spoken expansion behind a placeholder until other rules finish."""
        kept.append(text[match.start() : match.end()].replace_with(words))
        return SourceText(f" \x00{len(kept) - 1}\x00 ")

    text = text.sub(IMAGE_RE, lambda match: text[match.start(1) : match.end(1)])
    text = text.sub(CODE_SPAN_RE, lambda match: keep(match, speak_code(match.group(2))))
    text = text.sub(
        INLINE_MATH_RE,
        lambda match: keep(
            match, speak_tex(next(tex for tex in match.groups() if tex is not None))
        ),
    )
    text = text.sub(LINK_RE, lambda match: text[match.start(1) : match.end(1)])
    text = text.sub(URL_RE, lambda match: keep(match, _speak_url(match.group())))
    text = text.sub(TAG_RE, lambda _match: SourceText(", "))
    text = text.unescape_html()
    text = text.sub(
        ESCAPED_RE,
        lambda match: (
            keep(match, match.group(1))
            if match.group(1) in "#|"
            else text[match.start(1) : match.end(1)]
        ),
    )
    for pattern in (*[pattern for pattern, _ in EMPHASIS_RULES], PLURAL_RE):
        text = text.sub_group(pattern, 1)
    text = text.sub(CALL_RE, lambda match: keep(match, speak_code(match.group())))
    words: list[SourceText] = []
    for match in SPOKEN_WORD_RE.finditer(text.text):
        token = text[match.start() : match.end()]
        pronounced = _speak_word(token.text)
        if pronounced:
            start = token.text.find(pronounced)
            word = (
                token[start : start + len(pronounced)]
                if start >= 0
                else token.strip(SPOKEN_EDGES).replace_with(pronounced)
            )
            words.extend((word, SourceText(" ")))
    spoken = SourceText.join(words).strip()
    spoken = spoken.sub(PLACEHOLDER_RE, lambda match: kept[int(match.group(1))])
    return _tidy_text(spoken)


def _speech_unit(
    spoken: SourceText, start: int, end: int, pause: float, growing: bool
) -> SpeechUnit:
    """Package the normalized words and the Markdown ranges they came from."""
    text = spoken.text if growing else _finish(spoken.text)
    words = []
    for match in SPOKEN_WORD_RE.finditer(text):
        token = spoken[match.start() : match.end()].strip(SPOKEN_EDGES)
        if (source := token.span()) is not None and any(
            char.isalnum() for char in token.text
        ):
            words.append(SpeechWord(match.group(), source))
    return SpeechUnit(text, start, end, pause, growing, tuple(words))


def _speak_word(token: str) -> str:
    """Read one whitespace-separated word of prose.

    Args:
        token (str): The word with any punctuation around it.

    Returns:
        str: The words to speak.
    """
    if PLACEHOLDER_RE.fullmatch(token):
        return token
    lowered = token.lower()
    stripped = lowered.rstrip(",;:")
    if stripped in ABBREVIATIONS:
        return ABBREVIATIONS[stripped] + lowered[len(stripped) :]
    if token in PROSE_OPERATORS:
        return PROSE_OPERATORS[token]
    lead = re.match(r"^[\"'(\[{“‘]*", token)
    trail = re.search(r"[\"')\]}”’.,;:!?]*$", token)
    assert lead is not None and trail is not None
    core = token[lead.end() : max(lead.end(), trail.start())]
    punctuation = "".join(char for char in token[trail.start() :] if char in ".,;:!?")
    if not core:
        return punctuation
    if core.lower() in ABBREVIATIONS:
        return ABBREVIATIONS[core.lower()] + punctuation
    if unit := NUMBER_UNIT_RE.match(core):
        about = "about " if unit.group(1) else ""
        return f"{about}{unit.group(2)} {UNITS[unit.group(3).lower()]}{punctuation}"
    if span := RANGE_RE.match(core):
        return f"{span.group(1)} to {span.group(2)}{punctuation}"
    if core.startswith("#") and core[1:].isdigit():
        return f"number {core[1:]}{punctuation}"
    if core.startswith("#") and core[1:].isalnum():
        core = core[1:]
    if _looks_like_code(core):
        return speak_code(core) + punctuation
    if "/" in core and not core.startswith("/"):
        core = core.replace("/", " ")
    spoken = "".join(SYMBOL_WORDS.get(char, char) for char in core)
    return spoken.strip() + punctuation


def _looks_like_code(word: str) -> bool:
    """Check whether a word of prose is really code, such as a call, a path or an operator.

    Args:
        word (str): The word without surrounding punctuation.

    Returns:
        bool: Whether to read it as code.
    """
    if word.lower() in NO_SPLIT or re.fullmatch(r"(?:[A-Za-z]\.)+[A-Za-z]?", word):
        return False
    return bool(
        re.search(r"[A-Za-z0-9]_[A-Za-z0-9]|^_+[A-Za-z]", word)
        or re.search(r"[a-z][A-Z]", word)
        and word not in ("PhD", "iPhone", "iPad", "macOS", "iOS")
        or re.search(r"[A-Za-z_]\.[A-Za-z_]", word)
        or re.search(
            r"[\w)\]](?:[<>=!+*%^&|]+|::)[\w(\[\-]|[\w)\]](?:\+\+|--)$|^(?:\+\+|--|!)\w",
            word,
        )
        or (PATH_RE.match(word) and word.count("/") + word.count("\\") >= 2)
        or re.search(r"\w\[|[<>]=|[=!]=", word)
    )


def _split_identifier(name: str) -> str:
    """Read a camelCase, PascalCase or snake_case name as separate words.

    Args:
        name (str): The identifier.

    Returns:
        str: The words, with acronyms kept and common abbreviations expanded.
    """
    if name.lower() in ALIASES:
        return ALIASES[name.lower()]
    if "_" in name.strip("_") and name.isupper():
        name = (
            name.lower()
        )  # A constant such as MAX_SIZE is read as words, not spelled out
    words: list[str] = []
    for part in IDENTIFIER_PART_RE.findall(name.strip("_$")):
        lower = part.lower()
        if lower in ALIASES:
            words.append(ALIASES[lower])
        elif len(part) == 1 and part.isalpha():
            words.append(part.upper())  # A lone letter is read as its name
        elif part.isupper() and len(part) > 1:
            words.append(part if len(part) <= 4 else lower)
        else:
            words.append(lower)
    return " ".join(words)


def _speak_number(number: str) -> str:
    """Read a code number literal, such as hex or scientific notation.

    Args:
        number (str): The literal.

    Returns:
        str: The words to speak.
    """
    number = number.replace("_", "")
    if number[:2].lower() == "0x":
        return f"hex {' '.join(number[2:].upper())}"
    if scientific := re.fullmatch(r"(\d+(?:\.\d+)?)[eE]([+-]?\d+)", number):
        mantissa, exponent = scientific.groups()
        exponent = exponent.lstrip("+").replace("-", "minus ")
        prefix = "" if mantissa == "1" else f"{mantissa} times "
        return f"{prefix}10 to the {exponent}"
    return number


def _speak_url(url: str) -> str:
    """Read a link as its site's name, such as "github dot com".

    Args:
        url (str): The URL, perhaps in angle brackets.

    Returns:
        str: The words to speak.
    """
    host = re.sub(r"^<?(?:https?://)?(?:www\.)?", "", url).split("/")[0].rstrip(">.,")
    return " dot ".join(part for part in host.split(".") if part)


def _finish(spoken: str) -> str:
    """End a finished line with a full stop so the voice falls at its end.

    Args:
        spoken (str): The words of a heading, list item, row or sentence.

    Returns:
        str: The words with terminal punctuation.
    """
    spoken = spoken.rstrip(",;: ")
    return spoken if TERMINAL_RE.search(spoken) else f"{spoken}."


def _tidy(spoken: str) -> str:
    """Remove leftover markup, emoji and doubled spaces from spoken words.

    Args:
        spoken (str): The words.

    Returns:
        str: The cleaned words.
    """
    return _tidy_text(SourceText(spoken)).text


def _tidy_text(spoken: SourceText) -> SourceText:
    """Remove leftover marks from speech and carry the surviving source ranges."""
    spoken = spoken.sub(EMOJI_RE, lambda _match: SourceText(""))
    for pattern in (STRAY_MARKS_RE, re.compile(r"[#|<>{}\[\]^]"), SPACES_RE):
        spoken = spoken.sub(pattern, lambda _match: SourceText(" "))
    spoken = spoken.strip()
    for pattern in (re.compile(r"\s+([,.;:!?])"), re.compile(r"(,)(?:\s*,)+")):
        spoken = spoken.sub_group(pattern, 1)
    return spoken.sub(re.compile(r"^[,.;:\s]+"), lambda _match: SourceText(""))
