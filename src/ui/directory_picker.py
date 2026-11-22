from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QWidget


def choose_directory(
    parent: QWidget, current: str, title: str = "Choose Claude's directory"
) -> str:
    """Ask for a directory in a dialog excluded from screen capture like the overlay.

    Args:
        parent (QWidget): The widget that owns the dialog and keeps it above the overlay.
        current (str): The directory to start browsing from.
        title (str): The dialog title.

    Returns:
        str: The chosen directory, or an empty string when canceled.
    """
    dialog = QFileDialog(parent, title, current)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOptions(
        QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog
    )
    return _run(dialog)


def choose_file(
    parent: QWidget, title: str, current: str, name_filter: str, *, save_as: str = ""
) -> str:
    """Choose a file using the overlay's protected file dialog.

    Args:
        parent (QWidget): The widget that owns the dialog and keeps it above the overlay.
        title (str): The dialog title.
        current (str): The directory to start browsing from.
        name_filter (str): The file types to offer, e.g. "Documents (*.pdf *.txt)".
        save_as (str): The suggested file name when saving a new file.

    Returns:
        str: The chosen file, or an empty string when canceled.
    """
    dialog = QFileDialog(parent, title, current, name_filter)
    dialog.setOptions(QFileDialog.Option.DontUseNativeDialog)
    if save_as:
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix(Path(save_as).suffix.lstrip("."))
        dialog.selectFile(save_as)
    else:
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    return _run(dialog)


def _run(dialog: QFileDialog) -> str:
    """Show a picker hidden from screen capture and return its selection.

    Args:
        dialog (QFileDialog): The configured picker.

    Returns:
        str: The chosen path, or an empty string when canceled.
    """
    if dialog.exec() != QFileDialog.DialogCode.Accepted or not dialog.selectedFiles():
        return ""
    return str(Path(dialog.selectedFiles()[0]))
