def ensure_accessibility(prompt: bool = True) -> bool:
    """Report accessibility permission as granted.

    Windows requires no special permission to install a low-level keyboard hook,
    so this always reports success.

    Args:
        prompt (bool): Unused on Windows; present for interface parity.

    Returns:
        bool: Always True on Windows.
    """
    return True


def ensure_screen_recording(prompt: bool = True) -> bool:
    """Report screen-recording permission as granted.

    Windows requires no special permission to capture the screen, so this
    always reports success.

    Args:
        prompt (bool): Unused on Windows; present for interface parity.

    Returns:
        bool: Always True on Windows.
    """
    return True
