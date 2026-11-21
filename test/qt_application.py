"""Share one QApplication between all tests in a process."""

from functools import cache

from PyQt6.QtWidgets import QApplication


@cache
def application() -> QApplication:
    """Return the process's QApplication, creating it on first use and keeping it alive.

    Once a QApplication is destroyed, PyQt6 stops noticing when Qt deletes objects that Qt
    created itself, such as a scroll area's scroll bars. Their Python wrappers then outlive
    them, and a later object allocated at the same address is handed out under the stale
    wrapper's type, so calls on it corrupt memory or crash.

    Returns:
        QApplication: The application shared by every test in this process.
    """
    existing = QApplication.instance()
    return existing if isinstance(existing, QApplication) else QApplication([])
