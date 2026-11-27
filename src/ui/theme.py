from PyQt6.QtGui import QColor

PRIMARY_COLOR: tuple[int, int, int] = (255, 255, 255)
BG_COLOR: tuple[int, int, int] = (20, 20, 20)
WINDOW_ALPHA: int = 153
DANGER_COLOR: tuple[int, int, int] = (255, 77, 69)
WARNING_COLOR: tuple[int, int, int] = (255, 184, 77)
SPEAKING_COLOR: tuple[int, int, int] = (52, 199, 89)
SUCCESS_COLOR: tuple[int, int, int] = (52, 199, 89)
SPOKEN_WORD_COLOR: tuple[int, int, int] = (182, 223, 255)
PROVIDER_COLORS: dict[str, tuple[int, int, int]] = {
    "gemini": (138, 180, 248),
    "claude": (217, 119, 87),
}
LINK_COLOR: tuple[int, int, int] = (77, 170, 252)
# Darkens code blocks and table headers against the window fill while staying see-through
CODE_BG_ALPHA: int = 140
# Syntax colours based on VS Code Dark Modern, keyed by token role
CODE_COLORS: dict[str, tuple[int, int, int]] = {
    "text": (204, 204, 204),
    "comment": (132, 181, 112),
    "keyword": (86, 156, 214),
    "control": (197, 134, 192),
    "string": (206, 145, 120),
    "escape": (215, 186, 125),
    "regex": (209, 105, 105),
    "number": (181, 206, 168),
    "function": (220, 220, 170),
    "type": (78, 201, 176),
    "variable": (156, 220, 254),
    "constant": (79, 193, 255),
    "operator": (212, 212, 212),
}
# Bracket pair colours by nesting depth
BRACKET_COLORS: tuple[tuple[int, int, int], ...] = (
    (255, 215, 0),
    (218, 112, 214),
    (23, 159, 255),
)


def qcolor(rgb: tuple[int, int, int], alpha: int | None = None) -> QColor:
    """Convert an RGB tuple and optional alpha value to a QColor object.

    Args:
        rgb (tuple[int, int, int]): Store the red, green, and blue channel values.
        alpha (int | None): Store the optional alpha channel value.

    Returns:
        QColor: Return the converted Qt color object.
    """
    r, g, b = rgb
    c = QColor(r, g, b)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def qss(rgb: tuple[int, int, int], alpha: int | None = None) -> str:
    """Convert an RGB tuple and optional alpha value to a QSS string.

    Args:
        rgb (tuple[int, int, int]): Store the red, green, and blue channel values.
        alpha (int | None): Store the optional alpha channel value.

    Returns:
        str: Return the formatted QSS color string.
    """
    r, g, b = rgb
    if alpha is None:
        return f"rgb({r}, {g}, {b})"
    return f"rgba({r}, {g}, {b}, {alpha})"
