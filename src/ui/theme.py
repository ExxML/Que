from PyQt6.QtGui import QColor

PRIMARY_COLOR: tuple[int, int, int] = (255, 255, 255)
BG_COLOR: tuple[int, int, int] = (20, 20, 20)
WINDOW_ALPHA: int = 153
DANGER_COLOR: tuple[int, int, int] = (255, 77, 69)
WARNING_COLOR: tuple[int, int, int] = (255, 184, 77)
SPEAKING_COLOR: tuple[int, int, int] = (52, 199, 89)
PROVIDER_COLORS: dict[str, tuple[int, int, int]] = {
    "gemini": (138, 180, 248),
    "claude": (217, 119, 87),
}


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
