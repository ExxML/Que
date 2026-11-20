"""Check the browser media recorder with local fixture video, never claiming meeting coverage."""

import argparse
import base64
import importlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capture.backdrop import Backdrop
from capture.detector import identity, normalize
from capture.media import frames


def main() -> int:
    """Record a local fixture stream through the same receiver bridge and decode it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--browser", choices=("chrome", "msedge"), default="chrome")
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    media = directory / "receiver.webm"
    media.touch()
    backdrop = Backdrop(123456)
    backdrop.start()

    def chunk(encoded: str) -> None:
        """Persist each ordered chunk produced by the actual MediaRecorder."""
        with media.open("ab") as handle:
            handle.write(base64.b64decode(encoded, validate=True))

    playwright: Any = importlib.import_module("playwright.sync_api")
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(channel=args.browser, headless=True)
            try:
                page = browser.new_page(viewport={"width": 960, "height": 720})
                page.goto(backdrop.url)
                page.wait_for_timeout(500)
                page.evaluate("""async () => {
                    const video=document.createElement('video'); video.id='fixture-receiver';
                    video.srcObject=document.querySelector('canvas').captureStream(30);
                    video.style.cssText='position:fixed;right:0;bottom:0;width:160px;height:120px';
                    video.muted=true; document.body.append(video); await video.play();
                }""")
                page.expose_function("captureAuditChunk", chunk)
                page.evaluate(
                    Path(__file__).with_name("receiver.js").read_text(encoding="utf-8"),
                    {"selector": "#fixture-receiver", "canvasFPS": 30},
                )
                page.wait_for_timeout(5000)
                metadata = page.evaluate("() => window.captureAuditStop()")
                (directory / "receiver-state.json").write_text(
                    json.dumps(metadata, indent=2), encoding="utf-8"
                )
                print(json.dumps(metadata), flush=True)
            finally:
                browser.close()
    finally:
        backdrop.close()
    decoded = [identity(normalize(frame)) for frame, _ in frames(media)]
    ok = (
        len(decoded) >= 10
        and all(nonce == 123456 for nonce, _, _ in decoded)
        and len({tick for _, _, tick in decoded}) >= 5
        and not metadata["errors"]
    )
    report = {
        "scope": "Local fixture recorder transport only; no conference or Que capture validation.",
        "frames": len(decoded),
        "receiver": metadata,
        "pass": ok,
    }
    (directory / "smoke.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
