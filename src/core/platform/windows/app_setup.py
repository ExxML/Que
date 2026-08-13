from PyQt6.QtWidgets import QApplication


def configure_app(app: QApplication) -> None:
    """Apply platform application setup.

    On Windows the frameless Tool window already stays out of the taskbar, so no
    additional configuration is required.

    Args:
        app (QApplication): The running Qt application instance.
    """
    return None
