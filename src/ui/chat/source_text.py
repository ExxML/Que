"""Keep Markdown offsets through transformations used to render and read a reply."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

type SourceSpan = tuple[int, int]

_ENTITY = re.compile(r"&(?:#[xX][a-fA-F0-9]+|#\d+|[a-zA-Z][a-zA-Z0-9]*);?")
_HTML_SPECIAL = re.compile(r"[&<>]")
_HTML_PART = re.compile(r"<[^>]+>|[^<]+")
_VISIBLE_RUN = re.compile(r"\S+(?: \S+)*")


@dataclass(frozen=True)
class SourceText:
    """Represent transformed text with optional source offsets for each character."""

    text: str
    origins: tuple[SourceSpan | None, ...] = ()

    @classmethod
    def original(cls, text: str, start: int) -> SourceText:
        """Associate each character with its offset in the reply."""
        return cls(
            text, tuple((index, index + 1) for index in range(start, start + len(text)))
        )

    @classmethod
    def join(cls, parts: Iterable[SourceText]) -> SourceText:
        """Join parts and keep source offsets where they are available."""
        fragments = list(parts)
        text = "".join(part.text for part in fragments)
        if not any(part.origins for part in fragments):
            return cls(text)
        return cls(
            text,
            tuple(
                origin
                for part in fragments
                for origin in (part.origins or (None,) * len(part.text))
            ),
        )

    def __getitem__(self, key: slice) -> SourceText:
        """Slice the text and its source offsets together."""
        return SourceText(self.text[key], self.origins[key])

    def strip(self, chars: str | None = None) -> SourceText:
        """Strip edge characters and their offsets."""
        start = len(self.text) - len(self.text.lstrip(chars))
        return self[start : len(self.text.rstrip(chars))]

    def span(self) -> SourceSpan | None:
        """Return the covering Markdown span, if any characters have source offsets."""
        first = next((origin for origin in self.origins if origin is not None), None)
        if first is None:
            return None
        last = next(origin for origin in reversed(self.origins) if origin is not None)
        return first[0], last[1]

    def replace_with(self, replacement: str) -> SourceText:
        """Attribute replacement characters to this text's full source span."""
        span = self.span()
        return SourceText(replacement, (span,) * len(replacement) if span else ())

    def sub(
        self,
        pattern: re.Pattern[str],
        replacement: Callable[[re.Match[str]], SourceText],
    ) -> SourceText:
        """Apply a regex substitution that can preserve captured source offsets."""
        if not self.origins:
            return SourceText(
                pattern.sub(lambda match: replacement(match).text, self.text)
            )
        parts: list[SourceText] = []
        start = 0
        for match in pattern.finditer(self.text):
            parts.extend((self[start : match.start()], replacement(match)))
            start = match.end()
        parts.append(self[start:])
        return SourceText.join(parts)

    def sub_group(
        self, pattern: re.Pattern[str], group: int, before: str = "", after: str = ""
    ) -> SourceText:
        """Keep a captured range with optional generated text on each side."""
        return self.sub(
            pattern,
            lambda match: SourceText.join(
                (
                    SourceText(before),
                    self[match.start(group) : match.end(group)],
                    SourceText(after),
                )
            ),
        )

    def escape_html(self) -> SourceText:
        """Escape text while retaining each escaped character's origin."""
        if not self.origins:
            return SourceText(html.escape(self.text, quote=False))
        return self.sub(
            _HTML_SPECIAL,
            lambda match: self[match.start() : match.end()].replace_with(
                html.escape(match.group(), quote=False)
            ),
        )

    def unescape_html(self) -> SourceText:
        """Decode HTML entities and keep their source spans."""
        if not self.origins:
            return SourceText(html.unescape(self.text))
        return self.sub(
            _ENTITY,
            lambda match: self[match.start() : match.end()].replace_with(
                html.unescape(match.group())
            ),
        )


class RenderedSources:
    """Mark source ranges in a side copy of the reply's HTML for Qt to lay out."""

    def __init__(self) -> None:
        self.runs: list[SourceText] = []

    def mark(self, markup: SourceText) -> str:
        """Anchor each visible run without adding characters to the rendered text."""
        parts = []
        for match in _HTML_PART.finditer(markup.text):
            fragment = markup[match.start() : match.end()]
            if fragment.text.startswith("<"):
                parts.append(fragment.text)
                continue
            visible = fragment.unescape_html()
            start = 0
            for run in _VISIBLE_RUN.finditer(visible.text):
                word = visible[run.start() : run.end()]
                if word.span() is None:
                    continue
                parts.append(
                    html.escape(visible.text[start : run.start()], quote=False)
                )
                parts.append(f"<a name='que-source-{len(self.runs)}'></a>")
                parts.append(html.escape(word.text, quote=False))
                self.runs.append(word)
                start = run.end()
            parts.append(html.escape(visible.text[start:], quote=False))
        return "".join(parts)
