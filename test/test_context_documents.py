"""Verify document text extraction, storage, and once-per-conversation prompt context."""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from pypdf import PdfWriter
from PyQt6.QtWidgets import QApplication, QWidget

from core.ai_sender import AISender, InteractionRequest
from core.chat_history import ChatHistory
from core.claude_bridge import ClaudeBridge
from core.document_text import MAX_CHARACTERS, extract_text
from core.model_catalog import ModelOption
from core.settings import SettingsStore
from ui.window.settings_page import SettingsPage

WORD = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def text_pdf(text: str) -> bytes:
    """Build a one-page PDF that draws the given text.

    Args:
        text (str): ASCII text without parentheses or backslashes.

    Returns:
        bytes: The PDF file contents.
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return output


class ExtractTextTests(unittest.TestCase):
    """Read supported formats into compact text and reject unusable files."""

    def setUp(self) -> None:
        """Create a scratch directory for documents."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_text_whitespace_is_compacted(self) -> None:
        """Collapse runs of spaces and blank lines that only cost tokens."""
        path = self.root / "script.md"
        path.write_text("  Hello \t  world  \n\n\n\n  Next   line \n", encoding="utf-8")
        self.assertEqual(extract_text(path), "Hello world\n\nNext line")

    def test_docx_paragraphs_tabs_and_breaks(self) -> None:
        """Keep paragraph, tab, and line-break boundaries from Word documents."""
        path = self.root / "resume.DOCX"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                f"<w:document {WORD}><w:body>"
                "<w:p><w:r><w:t>Jane</w:t><w:tab/><w:t>Doe</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Line one</w:t><w:br/><w:t>Line two</w:t></w:r></w:p>"
                "</w:body></w:document>",
            )
        self.assertEqual(extract_text(path), "Jane Doe\nLine one\nLine two")

    def test_pdf_text(self) -> None:
        """Read selectable text from a PDF."""
        path = self.root / "letter.pdf"
        path.write_bytes(text_pdf("Dear hiring manager"))
        self.assertEqual(extract_text(path), "Dear hiring manager")

    def test_unusable_files_explain_the_problem(self) -> None:
        """Reject unsupported, corrupt, empty, scanned, and oversized files."""
        cases = {
            "resume.doc": (b"x", "Choose a PDF"),
            "broken.docx": (b"not a zip", "Could not read broken.docx"),
            "empty.txt": (b" \n\n ", "No text found in empty.txt"),
            "huge.txt": (b"a" * (MAX_CHARACTERS + 1), "huge.txt is too long"),
        }
        for name, (content, message) in cases.items():
            path = self.root / name
            path.write_bytes(content)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                extract_text(path)
        scanned = self.root / "scanned.pdf"
        writer = PdfWriter()
        writer.add_blank_page(612, 792)
        writer.write(scanned)
        with self.assertRaisesRegex(ValueError, "scanned PDF"):
            extract_text(scanned)


class ContextDocumentTests(unittest.TestCase):
    """Store documents and send them once at the start of each conversation."""

    def setUp(self) -> None:
        """Create an isolated settings store and disconnected sender."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(Path(self.directory.name) / "config.json")
        self.sender = AISender(self.store, client=MagicMock())
        self.sender.set_models((ModelOption("m", "M", ("default",)),))

    def test_save_remove_and_ignore_malformed(self) -> None:
        """Round-trip documents, remove one, and skip invalid saved entries."""
        self.store.save_context_document("resume", "cv.pdf", "Python developer")
        self.store.save_context_document("cover_letter", "letter.docx", "Dear team")
        self.store.save_context_document("cover_letter", "", "")
        self.assertEqual(
            SettingsStore(self.store.path).context_documents(),
            {"resume": ("cv.pdf", "Python developer")},
        )
        self.store.data["context_documents"] = {
            "resume": "bad",
            "unknown": {"name": "x", "text": "y"},
            "interview_script": {"name": "s.txt", "text": "Intro"},
        }
        self.assertEqual(
            self.store.context_documents(), {"interview_script": ("s.txt", "Intro")}
        )

    def test_documents_lead_only_the_first_turn(self) -> None:
        """Send documents in order on a new chat, not on continued turns."""
        self.assertEqual(self.sender.prepare_request("Hi").prompt, "Hi")
        self.store.save_context_document(
            "interview_script", "s.txt", "Tell me about you"
        )
        self.store.save_context_document("resume", "cv.pdf", "Built Que")
        first = self.sender.prepare_request("Question", transcript="[Them] Why us?")
        self.assertEqual(first.message, "Question")
        self.assertTrue(first.prompt.startswith("Background documents about me."))
        self.assertLess(
            first.prompt.index("<resume>\nBuilt Que\n</resume>"),
            first.prompt.index("<interview_script>"),
        )
        self.assertLess(
            first.prompt.index("</interview_script>"),
            first.prompt.index("<transcript>"),
        )
        self.assertTrue(first.prompt.endswith("\n\nQuestion"))
        self.assertTrue(self.sender.commit_interaction(first, "first-id"))
        self.assertEqual(self.sender.prepare_request("Next").prompt, "Next")
        self.sender.reset_chat()
        self.assertIn("<resume>", self.sender.prepare_request("New").prompt)

    def test_context_prompt_without_transcript(self) -> None:
        """Separate documents from the message when no transcript is pending."""
        request = InteractionRequest("Go", (), "m", "default", None, 0, context="Docs")
        self.assertEqual(request.prompt, "Docs\n\nGo")


class SettingsDocumentsTests(unittest.TestCase):
    """Upload and remove documents from the settings page."""

    def setUp(self) -> None:
        """Create a settings page backed by an isolated store."""
        self.app = QApplication.instance() or QApplication([])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SettingsStore(Path(self.directory.name) / "config.json")
        self.window = QWidget()
        self.addCleanup(self.window.deleteLater)
        self.page = SettingsPage(
            self.store,
            ClaudeBridge(),
            MagicMock(),
            ChatHistory(Path(self.directory.name) / "chat_history.json"),
            self.window,
        )
        self.addCleanup(self.page.key_timer.stop)

    def test_upload_show_and_remove(self) -> None:
        """Save picked text, show its size, report bad files, and remove it."""
        good = Path(self.directory.name) / "cv.txt"
        good.write_text("Senior Python developer", encoding="utf-8")
        bad = Path(self.directory.name) / "cv.doc"
        bad.write_text("x", encoding="utf-8")
        with patch("ui.window.settings_page.choose_file", return_value=str(good)):
            self.page._upload_document("resume")
        self.assertEqual(self.page.document_names["resume"].text(), "cv.txt · 3 words")
        self.assertTrue(self.page.document_remove_buttons["resume"].isEnabled())
        with patch("ui.window.settings_page.choose_file", return_value=str(bad)):
            self.page._upload_document("resume")
        self.assertIn("Choose a PDF", self.page.save_status.text())
        self.assertEqual(self.store.context_documents()["resume"][0], "cv.txt")
        with patch("ui.window.settings_page.choose_file", return_value=""):
            self.page._upload_document("resume")
        self.page.document_remove_buttons["resume"].click()
        self.assertEqual(self.store.context_documents(), {})
        self.assertEqual(self.page.document_names["resume"].text(), "")
        self.assertFalse(self.page.document_remove_buttons["resume"].isEnabled())


if __name__ == "__main__":
    unittest.main()
