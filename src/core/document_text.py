import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from pypdf import PdfReader

MAX_CHARACTERS = 50_000
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_text(path: Path) -> str:
    """Extract compact plain text from a PDF, Word, or text document.

    Plain text costs far fewer tokens than page images and works with every provider.
    Raise ValueError with a user-facing message when the file is unsupported,
    unreadable, empty, or too long.

    Args:
        path (Path): The document to read.

    Returns:
        str: The document text with redundant whitespace removed.
    """
    suffix = path.suffix.lower()
    if suffix not in (".pdf", ".docx", ".txt", ".md"):
        raise ValueError("Choose a PDF, Word (.docx), or text (.txt, .md) file.")
    try:
        if suffix == ".pdf":
            text = "\n\n".join(page.extract_text() for page in PdfReader(path).pages)
        elif suffix == ".docx":
            text = _docx_text(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as error:
        raise ValueError(
            f"Could not read {path.name}. Check that it opens, then try again."
        ) from error
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError(
            f"No text found in {path.name}. If it's a scanned PDF, export it with selectable text."
        )
    if len(text) > MAX_CHARACTERS:
        raise ValueError(
            f"{path.name} is too long ({len(text):,} characters). Use a file under {MAX_CHARACTERS:,} characters."
        )
    return text


def _docx_text(path: Path) -> str:
    """Read the paragraphs of a Word document without a Word dependency.

    Args:
        path (Path): The .docx file.

    Returns:
        str: One line per paragraph.
    """
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = []
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{WORD_NAMESPACE}t":
                parts.append(node.text or "")
            elif node.tag == f"{WORD_NAMESPACE}tab":
                parts.append(" ")
            elif node.tag in (f"{WORD_NAMESPACE}br", f"{WORD_NAMESPACE}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)
