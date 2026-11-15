"""Display an ordinary capture-visible native window as a positive control."""

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel


def main() -> int:
    """Keep a clearly identified native calibration surface visible until dismissed."""
    app = QApplication(sys.argv)
    label = QLabel(
        "QUE AUDIT POSITIVE CONTROL\nThis native surface must appear in capture.\nMove over the footprint, measure, then close."
    )
    label.setWindowTitle("Que audit capture calibration")
    label.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    label.setStyleSheet(
        "background: #ff00ff; color: #000000; font: bold 20px monospace; padding: 24px;"
    )
    label.resize(520, 180)
    label.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
