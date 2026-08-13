from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from PyQt6.QtWidgets import QApplication


def configure_app(_app: QApplication) -> None:
    """Apply platform application setup for macOS.

    Set the activation policy to Accessory so the app runs without a Dock icon
    or menu-bar application name, mirroring the Windows overlay's absence from
    the taskbar. The system-tray (menu-bar) item and overlay remain functional.
    """
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
    return None
