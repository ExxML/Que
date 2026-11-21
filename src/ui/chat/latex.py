import html
import re
from dataclasses import dataclass

from ui.theme import PRIMARY_COLOR, qss

MATH_FONT = "Cambria Math"
MATH_PT = 11.5  # Cambria Math looks smaller than Segoe UI at the same size
DISPLAY_PT = 12.5
LIMIT_PT = 8.5
BIG_OPERATOR_PT = 17.0
THIN = "\u2009"
MEDIUM = "\u2005"
QUAD = "\u2003"
QUAD_PX = 18

TOKEN_RE = re.compile(r"\\[a-zA-Z]+\*?|\\.|\s+|.", re.DOTALL)
TAG_RE = re.compile(r"<[^>]*>")
COMPLEX_RE = re.compile(r"[\s+−=<>,∗×⋅±∓÷/]")

# fmt: off
SYMBOLS = {
    # Greek
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Relations
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈",
    "sim": "∼", "simeq": "≃", "cong": "≅", "equiv": "≡", "propto": "∝", "ll": "≪", "gg": "≫",
    "prec": "≺", "succ": "≻", "preceq": "⪯", "succeq": "⪰", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "in": "∈", "notin": "∉", "ni": "∋", "mid": "∣",
    "parallel": "∥", "perp": "⊥", "models": "⊨", "vdash": "⊢", "coloneqq": "≔", "doteq": "≐",
    "leqslant": "⩽", "geqslant": "⩾", "lesssim": "≲", "gtrsim": "≳",
    # Arrows
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⟺", "implies": "⟹",
    "impliedby": "⟸", "mapsto": "↦", "longrightarrow": "⟶", "longleftarrow": "⟵",
    "Longrightarrow": "⟹", "uparrow": "↑", "downarrow": "↓", "hookrightarrow": "↪",
    "rightleftharpoons": "⇌",
    # Binary operators
    "times": "×", "cdot": "⋅", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆",
    "circ": "∘", "bullet": "∙", "oplus": "⊕", "otimes": "⊗", "odot": "⊙", "cup": "∪",
    "cap": "∩", "setminus": "∖", "wedge": "∧", "land": "∧", "vee": "∨", "lor": "∨",
    "sqcup": "⊔", "sqcap": "⊓", "uplus": "⊎", "dagger": "†",
    # Delimiters
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "langle": "⟨", "rangle": "⟩",
    "vert": "|", "lvert": "|", "rvert": "|", "Vert": "‖", "lVert": "‖", "rVert": "‖",
    "lbrace": "{", "rbrace": "}", "backslash": "\\",
    # Other
    "infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃", "nexists": "∄",
    "emptyset": "∅", "varnothing": "∅", "neg": "¬", "lnot": "¬", "ldots": "…", "dots": "…",
    "dotsc": "…", "dotsb": "⋯", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱", "angle": "∠",
    "triangle": "△", "square": "□", "prime": "′", "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ",
    "Im": "ℑ", "aleph": "ℵ", "therefore": "∴", "because": "∵", "top": "⊤", "bot": "⊥",
    "checkmark": "✓", "degree": "°", "colon": ":", "cdotp": "·", "S": "§", "P": "¶",
    "{": "{", "}": "}", "|": "‖", "%": "%", "$": "$", "#": "#", "&": "&", "_": "_",
}
SPACES = {",": THIN, ":": MEDIUM, ">": MEDIUM, ";": MEDIUM, " ": " ", "!": "", "quad": QUAD, "qquad": QUAD * 2}
RELATIONS = frozenset("=<>≤≥≠≈∼≃≅≡∝≪≫≺≻⪯⪰⊂⊆⊃⊇∈∉∋∣∥⊨⊢≔≐⩽⩾≲≳→←↔⇒⇐⇔⟺⟹⟸↦⟶⟵↑↓↪⇌")
BINARY = frozenset("+−×⋅÷±∓∗⋆∘∙⊕⊗⊙∪∩∖∧∨⊔⊓⊎")
OPENERS = frozenset("([{⌊⌈⟨|‖,")
FUNCTIONS = frozenset(
    ["arccos", "arcsin", "arctan", "arg", "cos", "cosh", "cot", "coth", "csc", "deg", "det", "dim", "exp", "gcd", "hom", "inf", "ker", "lg", "lim", "liminf", "limsup", "ln", "log", "max", "min", "Pr", "sec", "sin", "sinh", "sup", "tan", "tanh"]
)
LIMIT_FUNCTIONS = frozenset(["det", "gcd", "inf", "lim", "liminf", "limsup", "max", "min", "Pr", "sup"])
BIG_OPERATORS = {
    "sum": "∑", "prod": "∏", "coprod": "∐", "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⨁",
    "bigotimes": "⨂", "bigvee": "⋁", "bigwedge": "⋀", "bigsqcup": "⨆",
}
INTEGRALS = {"int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮"}
ACCENTS = {
    "hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303", "widetilde": "\u0303", "vec": "\u20d7",
    "dot": "\u0307", "ddot": "\u0308", "check": "\u030c", "breve": "\u0306", "acute": "\u0301",
    "grave": "\u0300",
}
TEXT_COMMANDS = {  # Command: CSS for its literal text argument
    "text": "", "textrm": "", "textnormal": "", "mbox": "", "mathrm": "", "operatorname": "",
    "operatorname*": "", "textsf": "", "mathsf": "", "textup": "", "textbf": "font-weight: bold;",
    "textit": "font-style: italic;", "emph": "font-style: italic;", "mathit": "font-style: italic;",
    "texttt": "font-family: Cascadia Mono, Consolas, monospace;", "mathtt": "font-family: Cascadia Mono, Consolas, monospace;",
}
IGNORED = frozenset(["displaystyle", "textstyle", "scriptstyle", "limits", "nolimits", "nonumber", "notag"])
SIZERS = frozenset(
    ["left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr", "bigm", "Bigm"]
)
# Mathematical alphabets: command -> (capital A, small a, letters with their own older code points)
ALPHABETS: dict[str, tuple[int, int, dict[str, str]]] = {
    "mathbb": (0x1D538, 0x1D552, {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}),
    "mathcal": (0x1D49C, 0x1D4B6, {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ",
                                   "M": "ℳ", "R": "ℛ", "e": "ℯ", "g": "ℊ", "o": "ℴ"}),
    "mathfrak": (0x1D504, 0x1D51E, {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"}),
}
ALPHABETS["mathscr"] = ALPHABETS["mathcal"]
ENVIRONMENT_DELIMITERS = {
    "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"), "vmatrix": ("|", "|"),
    "Vmatrix": ("‖", "‖"), "cases": ("{", ""), "dcases": ("{", ""), "rcases": ("", "}"),
}
# fmt: on


def render_math(tex: str, display: bool) -> str:
    """Render common LaTeX maths as rich text built from Unicode maths characters.

    Display maths stacks fractions, binomials and the limits of sums; inline maths keeps to one
    line so it flows with the text around it. Unknown commands show their name, and missing
    arguments render as empty, so half-streamed maths still shows.

    Args:
        tex (str): The LaTeX source between the maths delimiters.
        display (bool): Whether the maths stands on its own line.

    Returns:
        str: The maths as rich text.
    """
    parser = _Parser(tex, display)
    items, _stop = parser.parse(())
    size = DISPLAY_PT if display else MATH_PT
    return _layout(items, size)


@dataclass
class _Stack:
    """Hold rich text for a vertically stacked part of display maths, such as a fraction."""

    html: str


Item = str | _Stack


class _Parser:
    """Turn a LaTeX token stream into rich text items, one atom at a time."""

    def __init__(self, tex: str, display: bool) -> None:
        self.tokens: list[str] = TOKEN_RE.findall(tex)
        self.position = 0
        self.display = display
        self.tight = (
            False  # Inside a script, where TeX drops the space around operators
        )

    def parse(self, stops: tuple[str, ...]) -> tuple[list[Item], str | None]:
        """Parse atoms until one of the stop tokens or the end of the source.

        Args:
            stops (tuple[str, ...]): Tokens that end this run, such as a closing brace.

        Returns:
            tuple[list[Item], str | None]: The rendered items, and the stop token that ended
                them or None at the end of the source.
        """
        items: list[Item] = []
        while (token := self._next()) is not None:
            if token in stops:
                return items, token
            self._atom(token, items)
        return items, None

    def _next(self) -> str | None:
        """Take the next token that is not whitespace.

        Returns:
            str | None: The token, or None at the end of the source.
        """
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            self.position += 1
            if not token.isspace():
                return token
        return None

    def _peek(self) -> str | None:
        """Look at the next token that is not whitespace without taking it.

        Returns:
            str | None: The token, or None at the end of the source.
        """
        position = self.position
        token = self._next()
        self.position = position
        return token

    def _argument(self, display: bool | None = None) -> tuple[str, list[Item]]:
        """Parse a command's argument: a braced group or a single atom.

        Args:
            display (bool | None): Whether the argument may stack; None keeps the current mode.

        Returns:
            tuple[str, list[Item]]: The argument's LaTeX source and its rendered items.
        """
        outer = self.display
        if display is not None:
            self.display = display
        start = self.position
        items: list[Item] = []
        token = self._next()
        if token == "{":
            items, _stop = self.parse(("}",))
        elif token is not None:
            self._atom(token, items)
        self.display = outer
        raw = "".join(self.tokens[start : self.position]).strip()
        if raw.startswith("{"):
            raw = raw[1:].removesuffix("}")
        return raw, items

    def _script(self) -> str:
        """Parse a subscript or superscript argument as one tight line.

        Returns:
            str: The script as rich text.
        """
        outer = self.tight
        self.tight = True
        script = _flatten(self._argument(display=False)[1])
        self.tight = outer
        return script

    def _raw_group(self) -> str:
        """Take a braced group or single token as literal text.

        Returns:
            str: The text inside the group.
        """
        token = self._next()
        if token != "{":
            return token or ""
        depth, parts = 1, []
        while self.position < len(self.tokens):
            token = self.tokens[self.position]
            self.position += 1
            depth += {"{": 1, "}": -1}.get(token, 0)
            if depth == 0:
                break
            parts.append(token)
        return "".join(parts)

    def _optional(self) -> str:
        """Take a bracketed optional argument if one follows.

        Returns:
            str: The text inside the brackets, or an empty string.
        """
        if self._peek() != "[":
            return ""
        self._next()
        parts = []
        while (token := self._next()) not in (None, "]"):
            parts.append(token or "")
        return "".join(parts)

    def _scripts(self) -> tuple[str, str]:
        """Take any subscript and superscript that follow, in either order.

        Returns:
            tuple[str, str]: The subscript and superscript as one-line rich text.
        """
        scripts = {"_": "", "^": ""}
        while (token := self._peek()) in ("_", "^", "\\limits", "\\nolimits"):
            self._next()
            if token in scripts:
                scripts[str(token)] = self._script()
        return scripts["_"], scripts["^"]

    def _atom(self, token: str, items: list[Item]) -> None:
        """Render one token, and the arguments it takes, onto the items.

        Args:
            token (str): The token to render.
            items (list[Item]): The items rendered so far in this run.
        """
        name = token[1:] if token.startswith("\\") else ""
        if token == "{":
            group, _stop = self.parse(("}",))
            items.extend(group)
        elif token in ("^", "_"):
            script = self._script()
            items.append(
                f"<sup>{script}</sup>" if token == "^" else f"<sub>{script}</sub>"
            )
        elif token == "'":
            items.append("′")
        elif token in ("}", "&"):
            pass  # Stray closing brace or alignment outside an environment
        elif token == "~":
            items.append("&nbsp;")
        elif name in ("frac", "dfrac", "tfrac", "cfrac"):
            self._fraction(name, items)
        elif name in ("binom", "dbinom", "tbinom"):
            top, bottom = self._argument()[1], self._argument()[1]
            if self.display and name != "tbinom":
                items.extend(["(", _Stack(_stack([top, bottom])), ")"])
            else:
                items.append(f"C({_flatten(top)},{THIN}{_flatten(bottom)})")
        elif name == "sqrt":
            index = self._optional()
            _raw, radicand = self._argument()
            root = f"<sup>{html.escape(index)}</sup>√" if index else "√"
            if any(isinstance(item, _Stack) for item in radicand):
                items.extend([root, "(", *radicand, ")"])
            else:
                items.append(
                    f"{root}<span style='text-decoration: overline;'>{_flatten(radicand)}</span>"
                )
        elif name in BIG_OPERATORS or name in INTEGRALS:
            big = name in BIG_OPERATORS
            symbol = BIG_OPERATORS.get(name) or INTEGRALS[name]
            self._operator(symbol, items, limits=big, big=True)
        elif name in FUNCTIONS:
            self._operator(name, items, limits=name in LIMIT_FUNCTIONS, big=False)
        elif name in ("bmod", "mod"):
            items.append(f"{MEDIUM}mod{MEDIUM}")
        elif name == "pmod":
            items.append(
                f"{MEDIUM}(mod{THIN}{_flatten(self._argument(display=False)[1])})"
            )
        elif name in TEXT_COMMANDS:
            style = TEXT_COMMANDS[name]
            text = html.escape(self._raw_group(), quote=False)
            items.append(f"<span style='{style}'>{text}</span>" if style else text)
        elif name in ("mathbf", "boldsymbol", "bm", "pmb"):
            items.append(
                f"<b>{_flatten(self._argument(display=False)[1]).replace('<i>', '').replace('</i>', '')}</b>"
            )
        elif name in ALPHABETS:
            items.append(_alphabet(name, self._raw_group()))
        elif name in ACCENTS:
            items.append(f"{_flatten(self._argument(display=False)[1])}{ACCENTS[name]}")
        elif name in ("bar", "overline"):
            items.append(
                f"<span style='text-decoration: overline;'>{_flatten(self._argument(display=False)[1])}</span>"
            )
        elif name == "underline":
            items.append(f"<u>{_flatten(self._argument(display=False)[1])}</u>")
        elif name == "not":
            following = self._next() or ""
            negated = {
                "=": "≠",
                "\\in": "∉",
                "\\subset": "⊄",
                "\\equiv": "≢",
                "\\exists": "∄",
            }
            items.append(negated.get(following, f"{_character(following)}\u0338"))
        elif name == "begin":
            self._environment(self._raw_group(), items)
        elif name == "\\":
            self._optional()
            items.append("<br>")
        elif name in SPACES:
            items.append(SPACES[name])
        elif name in IGNORED:
            pass
        elif name in SIZERS:
            delimiter = self._next()
            if delimiter not in (None, "."):  # A dot is an invisible delimiter
                self._atom(str(delimiter), items)
        else:
            symbol = SYMBOLS.get(name, name) if name else token
            self._symbol(symbol, items)

    def _symbol(self, symbol: str, items: list[Item]) -> None:
        """Render a character or symbol, spacing relations and binary operators the way TeX does.

        Args:
            symbol (str): The character, or the text of an unknown command.
            items (list[Item]): The items rendered so far in this run.
        """
        symbol = "−" if symbol == "-" else "∗" if symbol == "*" else symbol
        escaped = html.escape(symbol, quote=False)
        if symbol in RELATIONS and not self.tight:
            items.append(f"{MEDIUM}{escaped}{MEDIUM}")
        elif symbol in BINARY and not self.tight and _follows_atom(items):
            items.append(f"{THIN}{escaped}{THIN}")
        elif symbol == "," and not self.tight:
            items.append(f",{THIN}")
        elif symbol.isascii() and symbol.isalpha():
            items.append(f"<i>{escaped}</i>")
        else:
            items.append(escaped)

    def _fraction(self, name: str, items: list[Item]) -> None:
        """Render a fraction, stacked in display maths and as a slash inline.

        Args:
            name (str): The fraction command, which may force a slash.
            items (list[Item]): The items rendered so far in this run.
        """
        numerator = self._argument()[1]
        denominator = self._argument()[1]
        if self.display and name != "tfrac":
            items.append(_Stack(_stack([numerator, denominator], rule=True)))
            return

        def side(rendered: list[Item]) -> str:
            """Bracket one side of an inline fraction when it has more than one term.

            Args:
                rendered (list[Item]): The side's rendered items.

            Returns:
                str: The side as rich text.
            """
            text = _flatten(rendered)
            return (
                f"({text})"
                if COMPLEX_RE.search(html.unescape(TAG_RE.sub("", text)))
                else text
            )

        items.append(f"{side(numerator)}/{side(denominator)}")

    def _operator(
        self, symbol: str, items: list[Item], limits: bool, big: bool
    ) -> None:
        """Render a big operator or named function with its scripts.

        In display maths, limits go above and below the operator; otherwise they are scripts.

        Args:
            symbol (str): The operator symbol or function name.
            items (list[Item]): The items rendered so far in this run.
            limits (bool): Whether display maths puts the scripts above and below.
            big (bool): Whether the symbol is drawn larger in display maths.
        """
        if _follows_atom(items):
            items.append(THIN)
        lower, upper = self._scripts()
        if self.display and limits and (lower or upper):
            operator = (
                f"<span style='font-size: {BIG_OPERATOR_PT}pt;'>{symbol}</span>"
                if big
                else symbol
            )
            small = f"<span style='font-size: {LIMIT_PT}pt;'>"
            parts: list[list[Item]] = [[f"{small}{upper}</span>"]] if upper else []
            parts.append([operator])
            if lower:
                parts.append([f"{small}{lower}</span>"])
            items.append(_Stack(_stack(parts)))
        else:
            if self.display and big:
                symbol = f"<span style='font-size: {BIG_OPERATOR_PT * 0.8}pt;'>{symbol}</span>"
            items.append(
                f"{symbol}{f'<sub>{lower}</sub>' if lower else ''}{f'<sup>{upper}</sup>' if upper else ''}"
            )
        if self._peek() not in (None, "(", "\\left", "^", "_", "}", "&", "\\\\"):
            items.append(THIN)

    def _environment(self, name: str, items: list[Item]) -> None:
        """Render a matrix, cases or alignment environment as a grid.

        Args:
            name (str): The environment name.
            items (list[Item]): The items rendered so far in this run.
        """
        name = name.removesuffix("*")
        if name == "array":
            self._raw_group()  # Column spec
        display = self.display
        aligned = name.startswith(("align", "split", "eqnarray"))
        # Like TeX, only alignments keep display style; matrix and cases cells use inline fractions
        self.display = display and (aligned or name.startswith("gather"))
        rows: list[list[list[Item]]] = [[]]
        while True:
            cell, stop = self.parse(("&", "\\\\", "\\end"))
            rows[-1].append(cell)
            if stop == "\\\\":
                self._optional()
                rows.append([])
            elif stop != "&":
                if stop == "\\end":
                    self._raw_group()
                break
        self.display = display
        rows = [row for row in rows if any(row_cell for row_cell in row)]
        opening, closing = ENVIRONMENT_DELIMITERS.get(name, ("", ""))
        if not display:
            text = "; ".join(" ".join(_flatten(cell) for cell in row) for row in rows)
            items.append(
                f"{opening}{text}{closing}" if opening != "{" else f"{{{THIN}{text}"
            )
            return
        left = name.endswith("cases") or name.startswith("gather")
        html_rows = []
        for row in rows:
            cells = []
            for column, cell in enumerate(row):
                align = (
                    ("right" if column % 2 == 0 else "left")
                    if aligned
                    else "left"
                    if left
                    else "center"
                )
                padding = f"0 {QUAD_PX if left and column == 0 else 6}px 2px 0"
                cells.append(
                    f"<td align='{align}' style='padding: {padding};'>{_layout(cell, DISPLAY_PT)}</td>"
                )
            html_rows.append(f"<tr>{''.join(cells)}</tr>")
        grid = f"<table cellspacing='0' cellpadding='0'>{''.join(html_rows)}</table>"
        size = f"font-size: {DISPLAY_PT * max(1, len(rows)) * 0.85}pt;"
        if opening:
            items.append(f"<span style='{size}'>{html.escape(opening)}</span>{THIN}")
        items.append(_Stack(grid))
        if closing:
            items.append(f"{THIN}<span style='{size}'>{html.escape(closing)}</span>")


def _character(token: str) -> str:
    """Look up the character a token stands for.

    Args:
        token (str): A command or a single character.

    Returns:
        str: The character as rich text.
    """
    return html.escape(
        SYMBOLS.get(token[1:], token[1:]) if token.startswith("\\") else token,
        quote=False,
    )


def _follows_atom(items: list[Item]) -> bool:
    """Check whether an operator here is binary rather than a sign, as in a − b versus −b.

    Args:
        items (list[Item]): The items rendered so far in this run.

    Returns:
        bool: Whether the previous item is something an operator can act on.
    """
    if not items:
        return False
    last = items[-1]
    if isinstance(last, _Stack):
        return True
    text = html.unescape(TAG_RE.sub("", last)).strip()
    return bool(text) and text[-1] not in RELATIONS | BINARY | OPENERS


def _flatten(items: list[Item]) -> str:
    """Join items into one line of rich text, keeping a stacked part's text in reading order.

    Args:
        items (list[Item]): The items to join.

    Returns:
        str: The joined rich text, with runs of italic letters merged.
    """
    return "".join(
        item if isinstance(item, str) else TAG_RE.sub(" ", item.html) for item in items
    ).replace("</i><i>", "")


def _layout(items: list[Item], size: float) -> str:
    """Lay out items in the maths font, putting stacked parts side by side with the text between them.

    Args:
        items (list[Item]): The items to lay out.
        size (float): The font size in points.

    Returns:
        str: The maths as rich text.
    """
    style = f"font-family: {MATH_FONT}; font-size: {size}pt;"
    if not any(isinstance(item, _Stack) for item in items):
        return f"<span style='{style}'>{_flatten(items)}</span>"
    cells: list[str] = []
    run: list[Item] = []
    for item in [*items, _Stack("")]:
        if isinstance(item, _Stack):
            if run:
                cells.append(f"<span style='{style}'>{_flatten(run)}</span>")
                run = []
            if item.html:
                cells.append(item.html)
        else:
            run.append(item)
    row = "".join(f"<td valign='middle'>{cell}</td>" for cell in cells)
    return (
        f"<table cellspacing='0' cellpadding='0' align='center'><tr>{row}</tr></table>"
    )


def _stack(parts: list[list[Item]], rule: bool = False) -> str:
    """Stack parts in a centred column, optionally with a fraction bar between the first two.

    Args:
        parts (list[list[Item]]): The parts from top to bottom.
        rule (bool): Whether to draw a fraction bar after the first part.

    Returns:
        str: The column as rich text.
    """
    rows = []
    for index, part in enumerate(parts):
        rows.append(
            f"<tr><td align='center' style='padding: 0 3px;'>{_layout(part, DISPLAY_PT)}</td></tr>"
        )
        if rule and index == 0:
            rows.append(
                f"<tr><td style='background-color: {qss(PRIMARY_COLOR, 220)}; font-size: 1pt;'>&nbsp;</td></tr>"
            )
    return f"<table cellspacing='0' cellpadding='0'>{''.join(rows)}</table>"


def _alphabet(name: str, text: str) -> str:
    """Write letters in a mathematical alphabet such as blackboard bold or calligraphic.

    Args:
        name (str): The alphabet command.
        text (str): The letters to convert.

    Returns:
        str: The converted letters as rich text.
    """
    capital, small, exceptions = ALPHABETS[name]
    converted = []
    for char in text:
        if char in exceptions:
            converted.append(exceptions[char])
        elif "A" <= char <= "Z":
            converted.append(chr(capital + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            converted.append(chr(small + ord(char) - ord("a")))
        else:
            converted.append(html.escape(char, quote=False))
    return "".join(converted)
