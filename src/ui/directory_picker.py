import ctypes
from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QWidget


def choose_directory(parent: QWidget, current: str) -> str:
    """Ask for a directory in a dialog excluded from screen capture like the overlay.

    Args:
        parent (QWidget): The widget that owns the dialog and keeps it above the overlay.
        current (str): The directory to start browsing from.

    Returns:
        str: The chosen directory, or an empty string when canceled.
    """
    # Qt cannot apply capture exclusion to the native Windows picker before it appears
    dialog = QFileDialog(parent, "Choose Claude's directory", current)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOptions(
        QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog
    )
    ctypes.windll.user32.SetWindowDisplayAffinity(int(dialog.winId()), 0x11)
    if dialog.exec() != QFileDialog.DialogCode.Accepted or not dialog.selectedFiles():
        return ""
    return str(Path(dialog.selectedFiles()[0]))
