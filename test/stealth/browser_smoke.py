"""Smoke-test the real observer page in headless Chromium without claiming desktop coverage."""

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_stealth import ASSETS, browser_path

from stealth.manifest import scenarios
from stealth.results import read_json
from stealth.server import EvidenceStore, ObserverServer


def main() -> int:
    """Verify JavaScript boot, actual HTTP delivery, and observer heartbeat in isolation."""
    from dataclasses import asdict

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cross-origin-isolated", action="store_true")
    args = parser.parse_args()
    executable = browser_path(args.browser)
    if executable is None:
        print("Chromium browser unavailable; smoke test is incomplete.")
        return 2
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "browser").mkdir()
    items = scenarios(read_json(ASSETS / "stealth" / "inventory.json"))
    store = EvidenceStore(directory, [])
    server = ObserverServer(
        store,
        ASSETS,
        {"scenarios": [asdict(item) for item in items]},
        args.cross_origin_isolated,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        process = subprocess.run(
            [
                str(executable),
                "--headless",
                "--dump-dom",
                "--no-first-run",
                "--virtual-time-budget=6000",
                f"--user-data-dir={directory / 'profile'}",
                f"{server.origin}/?browser={args.browser}#{server.token}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=45,
        )
        (directory / "rendered.html").write_text(process.stdout, encoding="utf-8")
        (directory / "browser.log").write_text(process.stderr, encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    events = [
        json.loads(line)
        for path in (directory / "browser").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    errors = [
        event for event in events if event["type"] in ("observer-error", "logger-error")
    ]
    ok = (
        process.returncode == 0
        and any(event["type"] == "observer-ready" for event in events)
        and sum(event["type"] == "heartbeat" for event in events) >= 2
        and not errors
        and any(
            event["type"] == "capabilities"
            and event["data"].get("crossOriginIsolated") == args.cross_origin_isolated
            for event in events
        )
    )
    print(
        json.dumps(
            {
                "smoke_pass": ok,
                "events": len(events),
                "errors": errors,
                "scope": "headless observer transport only; no physical stealth/capture validation",
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
