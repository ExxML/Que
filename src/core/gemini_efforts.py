import re
import urllib.request
from html.parser import HTMLParser


def parse_efforts(document: str) -> dict[str, tuple[str, ...]]:
    """Read Gemini's published thinking table in HTML or Markdown form.

    Args:
        document (str): The official thinking documentation.

    Returns:
        dict[str, tuple[str, ...]]: Explicit model-to-effort mappings.
    """
    parser = _TableParser()
    parser.feed(document)
    rows = parser.rows + [
        line.split("|")[1:-1]
        for line in document.splitlines()
        if line.strip().startswith("|")
    ]
    result: dict[str, tuple[str, ...]] = {}
    model_column = -1
    effort_column = -1
    for row in rows:
        normalized = [re.sub(r"\s+", " ", cell).strip() for cell in row]
        if any("Levels Supported" in cell for cell in normalized):
            model_column = next(
                (i for i, cell in enumerate(normalized) if cell.lower() == "model"), -1
            )
            effort_column = next(
                i for i, cell in enumerate(normalized) if "Levels Supported" in cell
            )
            continue
        if min(model_column, effort_column) < 0 or len(normalized) <= max(
            model_column, effort_column
        ):
            continue
        model_match = re.fullmatch(
            r"`?(gemini-[a-z0-9.\-]+)`?", normalized[model_column]
        )
        if model_match is None:
            continue
        levels = tuple(
            part.strip().strip("`").lower()
            for part in normalized[effort_column].split(",")
        )
        if levels and all(re.fullmatch(r"[a-z]+", level) for level in levels):
            result[model_match[1]] = tuple(dict.fromkeys(levels))
    return result


def load_efforts() -> dict[str, tuple[str, ...]]:
    """Fetch current effort capabilities from Gemini's thinking documentation.

    The Models API reports only whether a model thinks, so the documentation table is
    the sole published source of per-model levels. Nothing is stored between fetches.

    Returns:
        dict[str, tuple[str, ...]]: Published capabilities, or an empty mapping if unavailable.
    """
    request = urllib.request.Request(
        "https://ai.google.dev/gemini-api/docs/thinking",
        headers={"Accept": "text/markdown, text/html", "User-Agent": "Que"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return parse_efforts(response.read(2_000_000).decode("utf-8"))
    except (OSError, ValueError):
        return {}


class _TableParser(HTMLParser):
    """Extract table text without depending on the documentation site's styling."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        """Start a table row or cell.

        Args:
            tag (str): The HTML element name.
        """
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data: str) -> None:
        """Collect the current cell's text.

        Args:
            data (str): An HTML text fragment.
        """
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Complete a cell or row.

        Args:
            tag (str): The closing HTML element name.
        """
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr":
            self.rows.append(self.row)
