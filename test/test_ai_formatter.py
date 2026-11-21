"""Verify that model replies render as readable rich text in the chat."""

import unittest

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


class UserBubbleTests(unittest.TestCase):
    """Check user messages show exactly what was typed."""

    def setUp(self) -> None:
        """Create the shared application."""
        self.app = application()

    def test_user_text_is_not_markup(self) -> None:
        """Show tags typed by the user as text."""
        bubble = ChatBubble("<b>hi</b> & bye", is_user=True)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt; &amp; bye", bubble.message_label.text())


if __name__ == "__main__":
    unittest.main()
