import sys

from PyQt6.QtWidgets import QApplication

if sys.platform == "darwin":
    from core.platform.macos import app_setup as _backend
else:
    from core.platform.windows import app_setup as _backend


def configure_app(app: QApplication) -> None:
    """Apply platform application setup using the platform backend.

    Args:
        app (QApplication): The running Qt application instance.
    """
    _backend.configure_app(app)
