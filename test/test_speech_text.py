"""Tests that replies are read aloud the way a person would say them.

Que reads replies into the user's earbuds during an interview, so awkward speech, such as
spelled-out punctuation or a code block read character by character, would cost them the
thread of the answer.
"""

import unittest

from ui.chat.speech_text import (
    BLOCK_PAUSE,
    SENTENCE_PAUSE,
    source_offset,
    speak_code,
    speak_tex,
    speech_units,
    stable_end,
)


def spoken(markdown: str, complete: bool = True, start: int = 0) -> list[str]:
    """Return the sentences a reply is read as.

    Args:
        markdown (str): The reply.
        complete (bool): Whether it has finished streaming.
        start (int): The offset to read from.

    Returns:
        list[str]: The spoken sentences.
    """
    return [unit.text for unit in speech_units(markdown, complete, start)]


class CodeTests(unittest.TestCase):
    """Read inline code as words a listener understands."""

    def test_spec_examples(self) -> None:
        """Read a member call and a comparison naturally."""
        self.assertEqual(speak_code("fs.getFileSystem()"), "fs get file system")
        self.assertEqual(speak_code("a <= b"), "A less than or equal to B")

    def test_operators_are_words(self) -> None:
        """Speak every common operator instead of its symbols."""
        cases = {
            "a == b": "A equals B",
            "a === b": "A strictly equals B",
            "a != b": "A is not equal to B",
            "a >= b": "A greater than or equal to B",
            "a && !b": "A and not B",
            "a || b": "A or B",
            "x => x * 2": "X arrow X times 2",
            "i++": "I plus plus",
            "n += 1": "N plus equals 1",
            "a % b": "A mod B",
            "a << 2": "A shift left 2",
            "a ^ b": "A xor B",
            "n ** 2": "N squared",
            "n ** k": "N to the power of K",
            "2^n": "2 to the power of N",
            "n^3": "N cubed",
            "-1": "minus 1",
            "**kwargs": "keyword arguments",
            "a?.b": "A B",
        }
        for code, words in cases.items():
            with self.subTest(code=code):
                self.assertEqual(speak_code(code), words)

    def test_names_split_into_words(self) -> None:
        """Split camel, Pascal, snake and screaming case, keeping acronyms."""
        cases = {
            "getFileSystem": "get file system",
            "HTTPServer": "HTTP server",
            "user_id": "user ID",
            "MAX_SIZE": "max size",
            "_cache": "cache",
            "__init__": "init",
            "len": "length",
            "utf8": "utf 8",
            "XMLHttpRequestFactory": "XML http request factory",
        }
        for code, words in cases.items():
            with self.subTest(code=code):
                self.assertEqual(speak_code(code), words)

    def test_brackets_calls_and_generics(self) -> None:
        """Read calls as "of", indexes as "at" and generics as "of"."""
        cases = {
            "len(nums) - 1": "length of numbers minus 1",
            "dict.get(key, default)": "dict get of key, default",
            "arr[i][j]": "array at I at J",
            "List<String>": "list of string",
            "std::vector<int>": "standard vector of int",
            "foo()": "foo",
            "{}": "",
            "[1, 2]": "1, 2",
            "(a + b)": "A plus B",
            "O(n log n)": "O of N log N",
        }
        for code, words in cases.items():
            with self.subTest(code=code):
                self.assertEqual(speak_code(code), words)

    def test_files_paths_and_literals(self) -> None:
        """Read file names with "dot", paths by their last part, and literals plainly."""
        cases = {
            "main.py": "main dot pie",
            "src/core/speaker.py": "speaker dot pie",
            "C:\\Users\\me\\notes.txt": "notes dot text",
            "src/": "source",
            ".env": "dot env",
            "'hello world'": "hello world",
            "0x1F": "hex 1 F",
            "1e9": "10 to the 9",
            "2.5e-3": "2.5 times 10 to the minus 3",
            "1_000": "1000",
            "a § b": "A B",
            "x: int; y": "X, int, Y",
        }
        for code, words in cases.items():
            with self.subTest(code=code):
                self.assertEqual(speak_code(code), words)


class MathTests(unittest.TestCase):
    """Read LaTeX maths as spoken maths."""

    def test_formulas(self) -> None:
        """Read fractions, roots, powers, limits and symbols."""
        cases = {
            r"\frac{a}{b}": "A over B",
            r"\binom{n}{k}": "N choose K",
            r"\sqrt{n}": "the square root of N",
            r"\sqrt[3]{x}": "the 3th root of X",
            r"x^2 + y^{3}": "X squared plus Y cubed",
            r"A^T": "A transpose",
            r"e^{i\pi}": "E to the power of I pi",
            r"a_i": "A sub I",
            r"\log_2 n": "log base 2 of N",
            r"\sum_{i=1}^{n} i": "the sum from I equals 1 to N of I",
            r"\int f": "the integral of F",
            r"\lim_{x \to 0} f": "the limit as X to 0 of F",
            r"\lim f": "the limit of F",
            r"a \equiv b \pmod{m}": "A is equivalent to B mod M",
            r"\mathcal{O}(n)": "big O N",
            r"\text{if } x \le 3.5": "if X less than or equal to 3.5",
            r"\mathbf x": "X",
            r"\alpha + \varepsilon": "alpha plus epsilon",
            r"\begin{cases} a \\ b \end{cases}": "A, B",
            r"a \, b \quad c": "A B C",
            r"\foo x": "foo X",
            r"n! .": "N factorial",
            r"}$": "",
            r"{a} + b": "A plus B",
            r"x^": "X to the power of",
        }
        for tex, words in cases.items():
            with self.subTest(tex=tex):
                self.assertEqual(speak_tex(tex), words)


class ProseTests(unittest.TestCase):
    """Read Markdown prose without markup, symbols or emoji."""

    def test_inline_markup(self) -> None:
        """Read code spans, maths, links and emphasis by their meaning."""
        self.assertEqual(
            spoken("Use `fs.getFileSystem()` and **bold** _text_ with $x^2$."),
            ["Use fs get file system and bold text with X squared."],
        )
        self.assertEqual(
            spoken(
                "See [the docs](https://example.com/x), ![a chart](c.png) and https://github.com/a/b."
            ),
            ["See the docs, a chart and github dot com."],
        )
        self.assertEqual(
            spoken(
                "Line<br>break &amp; \\*stars\\* and \\# hash \u2014 done \U0001f680"
            ),
            ["Line, break and stars and hash, done."],
        )

    def test_words_people_say_differently(self) -> None:
        """Expand abbreviations, units, ranges and symbols."""
        cases = {
            "It is 2x faster, ~10ms, e.g. for 1-3 items.": "It is 2 times faster, about 10 milliseconds, for example for 1 to 3 items.",
            "We use C++ and C# vs. Java & Go.": "We use C plus plus and C sharp versus Java and Go.",
            "Item #1 is #important.": "Item number 1 is important.",
            "The file(s) are big and/or small.": "The files are big and or small.",
            "Check a <= b then x -> y.": "Check a less than or equal to b then x to y.",
            "Call fs.readFile then O(n) work.": "Call fs read file then O of N work.",
            "Edit src/core/x.py today.": "Edit X dot pie today.",
            "Use useEffect() in React, i.e. hooks.": "Use use effect in React, that is hooks.",
            "Cost ≤ 5 → done.": "Cost less than or equal to 5 to done.",
            'It\'s ("quoted") here .': "It's quoted here.",
            "Read /etc/hosts.": "Read hosts.",
            "Rust (vs Go) wins.": "Rust versus Go wins.",
        }
        for markdown, words in cases.items():
            with self.subTest(markdown=markdown):
                self.assertEqual(spoken(markdown), [words])

    def test_sentences_split_at_full_stops_only(self) -> None:
        """Split sentences, but not at abbreviations, initials, decimals or inside code."""
        self.assertEqual(
            spoken("Pi is 3.14. Use `a. b` now! See U.S. law, e.g. here. Done?"),
            ["Pi is 3.14.", "Use A B now!", "See U.S. law, for example here.", "Done?"],
        )


class BlockTests(unittest.TestCase):
    """Read the blocks of a reply, never its code blocks."""

    def test_code_blocks_are_never_spoken(self) -> None:
        """Skip fenced code, closed or still open."""
        self.assertEqual(
            spoken(
                "Before.\n\n```python\nprint('x')\n```\n\nAfter.\n\n~~~\nopen fence"
            ),
            ["Before.", "After."],
        )

    def test_structure_reads_as_sentences(self) -> None:
        """Read headings, list items, tasks, quotes and tables as separate sentences."""
        markdown = (
            "## Approach\n\n1. Use a **hash map**\n- [ ] task item\n> Quote here\n\n---\n"
            "| Name | Cost |\n|---|---|\n| HashMap | O(1) |\n| `TreeMap` | O(log n) |\n\n"
            "First line\nsecond line.\n\n$$\\frac{1}{2}\n$$\n"
        )
        units = speech_units(markdown, True)
        self.assertEqual(
            [unit.text for unit in units],
            [
                "Approach.",
                "Use a hash map.",
                "task item.",
                "Quote here.",
                "Name, Cost.",
                "hash map, O of 1.",
                "tree map, O of log N.",
                "First line.",
                "second line.",
                "1 over 2.",
            ],
        )
        self.assertEqual(units[0].pause, BLOCK_PAUSE)
        self.assertEqual(units[7].pause, SENTENCE_PAUSE)

    def test_unspeakable_blocks_are_skipped(self) -> None:
        """Leave out rows, formulas and lines with nothing to say."""
        self.assertEqual(
            spoken("| \U0001f680 | |\n|---|---|\n\n$$ $$\n\n\U0001f680"), []
        )

    def test_reading_from_an_offset(self) -> None:
        """Start mid-sentence at the offset, but never inside a code span."""
        markdown = "First sentence. Then use `foo bar` here."
        self.assertEqual(
            spoken(markdown, start=markdown.index("Then")), ["Then use foo bar here."]
        )
        self.assertEqual(
            spoken(markdown, start=markdown.index("bar")), ["foo bar here."]
        )
        self.assertEqual(spoken("One.\n\nTwo.", start=6), ["Two."])


class StreamingTests(unittest.TestCase):
    """Read only what later chunks of a streaming reply cannot change."""

    def test_stable_text(self) -> None:
        """Stop before half words and half-written markup."""
        cases = {
            "Hello wor": "Hello ",
            "Hello": "",
            "Text `code an": "Text ",
            "Text \\(x an": "Text ",
            "Math $x an": "Math ",
            "Price $5 now": "Price $5 ",
            "A [link te": "A ",
            "A [link](http://x.y and": "A ",
            "A [b] c d": "A [b] c ",
            "Done.\n```py": "Done.\n",
            "Done.\n| a | b": "Done.\n",
            "Head | er\n|---": "",
            "| a | b |\n| c | d": "| a | b |\n",
            "x\n|---|---|\n| a | b |\n| c": "x\n|---|---|\n| a | b |\n",
        }
        for markdown, stable in cases.items():
            with self.subTest(markdown=markdown):
                self.assertEqual(markdown[: stable_end(markdown)], stable)

    def test_growing_sentence_is_open(self) -> None:
        """Mark a sentence the stream may still extend, and close it at its full stop."""
        units = speech_units("Stream: the answer is", False)
        self.assertEqual(
            [(unit.text, unit.open) for unit in units], [("Stream: the answer", True)]
        )
        units = speech_units("The answer is 42. Next", False)
        self.assertEqual(
            [(unit.text, unit.open) for unit in units], [("The answer is 42.", False)]
        )

    def test_rows_and_formulas_wait_for_their_end(self) -> None:
        """Read a table row or a formula once, when its line is finished."""
        self.assertEqual(spoken("| a | b |\n|---|---|\n| c | d ", False), ["a, b."])
        self.assertEqual(spoken("Sum:\n$$\\frac{a}{b} ", False), ["Sum."])
        self.assertEqual(spoken("Sum:\n$$\\frac{a}{b}", True), ["Sum.", "A over B."])
        self.assertEqual(spoken("Sum:\n$$\n\\frac{a}{b}\n", False), ["Sum."])
        self.assertEqual(spoken("a | b\n---|---\nc | d ", False), ["a, b."])


class SourceOffsetTests(unittest.TestCase):
    """Map a clicked line of the shown reply back to its Markdown."""

    def test_line_maps_to_its_markdown(self) -> None:
        """Find the clicked line, counting earlier repeats of its words."""
        markdown = "Say **hi there** now.\n\nSay hi there again, later."
        rendered = "Say hi there now.\nSay hi there again, later."
        offset = source_offset(markdown, rendered, rendered.index("Say hi there again"))
        self.assertEqual(markdown[offset:], "Say hi there again, later.")

    def test_click_in_a_code_block_reads_after_it(self) -> None:
        """Start after a code block when the click lands in it."""
        markdown = "Intro.\n\n```\ncode x\n```\n\nAfter the code."
        rendered = "Intro.\ncode x\nAfter the code."
        offset = source_offset(markdown, rendered, rendered.index("code x"))
        self.assertEqual(spoken(markdown, start=offset), ["After the code."])

    def test_fallbacks(self) -> None:
        """Fall back to shorter word runs, then to the same share of the text."""
        self.assertEqual(source_offset("alpha beta gamma", "x beta", 2), 6)
        self.assertEqual(source_offset("alpha beta", "beta beta", 5), 6)
        self.assertEqual(source_offset("one two", "zeta two", 0), 0)
        self.assertEqual(source_offset("abcdefgh", "∑∑∑∑", 2), 4)
        self.assertEqual(source_offset("abc", "", 0), 0)


if __name__ == "__main__":
    unittest.main()
