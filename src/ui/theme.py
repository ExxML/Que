from PyQt6.QtGui import QColor, QFont

PRIMARY_COLOR: tuple[int, int, int] = (255, 255, 255)
BG_COLOR: tuple[int, int, int] = (20, 20, 20)


def ui_font_family() -> str:
    """Return the UI sans-serif font family.

    Tahoma is the closest match to Microsoft JhengHei (the original design
    font) that ships on both Windows and macOS, so both platforms render the
    same humanist sans-serif UI text.

    Returns:
        str: The font family name to use for UI text.
    """
    return "Tahoma"


def ui_font(pixel_size: int) -> QFont:
    """Return the UI font at a fixed pixel size.

    Sizing in pixels rather than points keeps text the same visual size on
    Windows (96 dpi point scaling) and macOS (72 dpi point scaling), where the
    same point size would render noticeably smaller.

    Args:
        pixel_size (int): The font height in pixels.

    Returns:
        QFont: The UI font at the requested pixel size.
    """
    font = QFont(ui_font_family())
    font.setPixelSize(pixel_size)
    return font


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
