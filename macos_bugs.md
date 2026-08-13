# Bugs on macOS
- Main window is not always-on-top- clicking on different windows brings that window to the front, and covers the Que main window.
- When the user takes a screenshot, it seems to screenshot the wallpaper, not any of the windows.
- The font is too small. What's the closest font to Microsoft JhengHei that is available on both macOS and Windows? Use that font.

# General
- Keyboard modifiers (Shift, Ctrl, Alt, etc.) do not get suppressed; it should use the old functionality in win32_hook.py and shortcut_manager.py, before the staged changes were implemented. The change should work on both macOS and Windows.
