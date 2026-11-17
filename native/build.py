"""Build src/assets/que_hook.dll from native/que_hook.c with MSVC, or with gcc when MSVC is absent.

Run from the repository root after changing que_hook.c, then commit both files:

    python native/build.py
"""

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "que_hook.c"
OUTPUT = ROOT / "src" / "assets" / "que_hook.dll"
VSWHERE = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")


def main() -> int:
    """Compile the hook library and report which compiler built it.

    Returns:
        int: Zero on success, otherwise one.
    """
    vcvars = _find_vcvars()
    # Stamped into the DLL so tests can tell when it was not rebuilt after an edit
    digest = source_hash()
    with tempfile.TemporaryDirectory() as work:
        if vcvars is not None:
            # Static runtime: the DLL depends only on system libraries
            # A script file avoids cmd.exe's quoting rules for paths with spaces
            # /Brepro: timestamps become a content hash, so an unchanged source rebuilds byte-identical
            script = Path(work) / "build.cmd"
            script.write_text(
                f'@call "{vcvars}" >nul 2>&1\r\n'
                f'@cl /nologo /Brepro /O2 /W4 /WX /MT /GS /guard:cf /LD /DQUE_SOURCE_SHA256=\\"{digest}\\" '
                f'"{SOURCE}" /Fe:que_hook.dll '
                "/link /Brepro /DYNAMICBASE /NXCOMPAT /guard:cf user32.lib kernel32.lib\r\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["cmd", "/d", "/c", str(script)], cwd=work, check=False
            )
        elif shutil.which("gcc"):
            result = subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-shared",
                    "-static",
                    f'-DQUE_SOURCE_SHA256="{digest}"',
                    "-o",
                    str(Path(work) / "que_hook.dll"),
                    str(SOURCE),
                    "-luser32",
                ],
                check=False,
            )
        else:
            print("No C compiler found: install Visual Studio Build Tools (C++).")
            return 1
        if result.returncode != 0:
            return 1
        try:
            shutil.copyfile(Path(work) / "que_hook.dll", OUTPUT)
        except PermissionError:
            print(
                f"{OUTPUT.name} is in use: quit Que and any running tests, then retry."
            )
            return 1
    print(f"Built {OUTPUT.relative_to(ROOT)} with {'MSVC' if vcvars else 'gcc'}.")
    return 0


def source_hash() -> str:
    """Hash que_hook.c independent of line endings, so a CRLF checkout matches an LF one.

    Returns:
        str: The SHA-256 hex digest.
    """
    text = SOURCE.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_vcvars() -> Path | None:
    """Locate the newest MSVC x64 environment script.

    Returns:
        Path | None: vcvars64.bat, or None when no MSVC C++ toolset is installed.
    """
    if not VSWHERE.is_file():
        return None
    found = subprocess.run(
        [
            str(VSWHERE),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        capture_output=True,
        check=False,
        text=True,
    ).stdout.strip()
    script = (
        Path(found) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat" if found else None
    )
    return script if script is not None and script.is_file() else None


if __name__ == "__main__":
    sys.exit(main())
