"""Verify that model replies render as readable rich text in the chat."""

import unittest

from PyQt6.QtGui import QFont, QTextDocument
from qt_application import application

from ui.chat.ai_formatter import format_message
from ui.chat.chat_bubble import ChatBubble
from ui.theme import CODE_COLORS, qss


class FormatMessageTests(unittest.TestCase):
    """Check the Markdown that models write is converted to the right rich text."""

    def test_escapes_markup_in_text_and_code(self) -> None:
        """Show angle brackets and ampersands literally instead of as markup."""
        html = format_message("a <b>tag</b> & `<i>` and\n```\n<script>\n```")
        self.assertNotIn("<b>tag", html)
        self.assertIn("&lt;b&gt;tag&lt;/b&gt; &amp;", html)
        self.assertIn("&lt;i&gt;", html)
        self.assertIn("&lt;script&gt;", html)

    def test_inline_emphasis_ignores_identifiers_and_math(self) -> None:
        """Format bold, italic and strikethrough without touching snake_case or products."""
        html = format_message("**bold** *it* _em_ ~~gone~~ snake_case_name 2*3*4")
        for expected in (
            "<b>bold</b>",
            "<i>it</i>",
            "<i>em</i>",
            "<s>gone</s>",
            "snake_case_name",
            "2*3*4",
        ):
            self.assertIn(expected, html)

    def test_unclosed_fence_streams_as_code(self) -> None:
        """Highlight a code block whose closing fence has not arrived yet."""
        html = format_message("Here:\n```python\nreturn x")
        self.assertIn(f"color: {qss(CODE_COLORS['control'])};'>return", html)
        self.assertIn(">python<", html)

    def test_unknown_language_is_plain(self) -> None:
        """Show code in a language Pygments does not know without colouring it."""
        html = format_message("```notalanguage\nif x < 1\n```")
        self.assertIn("if x &lt; 1", html)
        self.assertNotIn(qss(CODE_COLORS["control"]), html)

    def test_nested_and_ordered_lists(self) -> None:
        """Nest indented items with their own bullet and keep numbering from the first item."""
        html = format_message("- outer\n  - inner\n\n3. three\n1. four")
        self.assertIn("•", html)
        self.assertIn("◦", html)
        self.assertLess(html.index("outer"), html.index("◦"))
        self.assertIn(">3.</td>", html)
        self.assertIn(">4.</td>", html)

    def test_list_item_keeps_its_indented_code(self) -> None:
        """Keep a fenced block indented under a numbered step inside that step."""
        html = format_message("1. Step\n   ```js\n   let a = 1;\n   ```\n2. Next")
        self.assertLess(html.index("let"), html.index(">2.</td>"))

    def test_table_cells_and_alignment(self) -> None:
        """Render a GitHub table with its header, cells and column alignment."""
        html = format_message("| Name | Cost |\n|---|---:|\n| a | **1** |")
        self.assertIn("<td align='right'", html)
        self.assertIn("<b>1</b>", html)
        self.assertNotIn("---", html)

    def test_headings_rules_and_quotes(self) -> None:
        """Turn headings, rules and quotes into blocks instead of literal symbols."""
        html = format_message("## Title\ntext\n\n---\n\n> quoted")
        self.assertIn("font-weight: bold;'>Title</p>", html)
        self.assertNotIn("##", html)
        self.assertNotIn("---", html)
        self.assertNotIn("&gt; quoted", html)


class MathTests(unittest.TestCase):
    """Check that LaTeX maths renders as readable Unicode maths."""

    def test_inline_symbols_scripts_and_fractions(self) -> None:
        """Turn commands into symbols, scripts into sup/sub, and inline fractions into slashes."""
        html = format_message(
            r"$\alpha \le \sqrt{n}$ and $x_i^2$ and \(\frac{n(n+1)}{2}\)"
        )
        for expected in ("α", "≤", "√", "<sub>", "<sup>2</sup>", ")/2", "Cambria Math"):
            self.assertIn(expected, html)
        self.assertNotIn("\\", html)

    def test_prices_stay_text(self) -> None:
        """Leave dollar amounts alone and show escaped dollar signs."""
        html = format_message(r"It costs $5 and $10, or \$3.")
        self.assertIn("$5 and $10, or $3.", html)
        self.assertNotIn("Cambria Math", html)

    def test_display_math_stacks_fractions_and_limits(self) -> None:
        """Stack fractions and sum limits in a centred display block across several lines."""
        html = format_message("Before\n$$\n\\sum_{i=1}^{n} i = \\frac{a}{b}\n$$\nAfter")
        self.assertIn("<td align='center'>", html)
        self.assertIn("∑", html)
        self.assertNotIn("a/b", html)
        self.assertLess(html.index("∑"), html.index("After"))

    def test_environment_and_half_streamed_display_math(self) -> None:
        """Lay out cases as rows, and still render display maths that has not been closed."""
        html = format_message(
            r"\[ f = \begin{cases} 1 & x \\ 0 & \text{otherwise} \end{cases} \]"
        )
        self.assertIn("otherwise", html)
        self.assertIn("{", html)
        self.assertIn("1/", format_message(r"$$\tfrac{1}{"))

    def test_math_inside_code_stays_literal(self) -> None:
        """Show dollar signs inside code exactly as written."""
        html = format_message("`echo $HOME $PATH`\n```bash\necho $x$\n```")
        self.assertNotIn("Cambria Math", html)


class ChatBubbleTests(unittest.TestCase):
    """Check that chat bubbles render messages with the intended text and typography."""

    def setUp(self) -> None:
        """Create the shared application."""
        self.app = application()

    def test_user_text_is_not_markup(self) -> None:
        """Show tags typed by the user as text."""
        bubble = ChatBubble("<b>hi</b> & bye", is_user=True)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt; &amp; bye", bubble.message_label.text())

    def test_chat_typography(self) -> None:
        """Render prompts and streamed prose in 12 pt regular with readable code and math."""
        prompt = ChatBubble("Prompt", is_user=True)
        reply = ChatBubble("")
        reply.set_bot_message(
            "## Heading\n\nProse with `inline_code` and $\\alpha$.\n\n```python\n# comment"
        )
        for bubble in (prompt, reply):
            self.assertEqual(bubble.message_label.font().pointSizeF(), 12.0)
            self.assertEqual(bubble.message_label.font().weight(), QFont.Weight.Normal)

        document = QTextDocument()
        document.setDefaultFont(reply.message_label.font())
        document.setHtml(reply.message_label.text())
        for sample, points in (
            ("Heading", 14.0),
            ("Prose", 12.0),
            ("inline_code", 11.0),
            ("α", 13.0),
            ("python", 10.0),
            ("# comment", 11.0),
        ):
            with self.subTest(sample=sample):
                cursor = document.find(sample)
                self.assertFalse(cursor.isNull())
                self.assertEqual(cursor.charFormat().fontPointSize(), points)
        self.assertEqual(
            document.find("Prose").charFormat().fontWeight(), QFont.Weight.Normal
        )


if __name__ == "__main__":
    unittest.main()
