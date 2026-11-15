"""Check real OPFS and memory workers through an isolated headless browser debugger."""

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_stealth import ASSETS, browser_path

from stealth.manifest import scenarios
from stealth.results import read_json
from stealth.server import EvidenceStore, ObserverServer


def main() -> int:
    """Run actual worker operations with synthetic clicks limited to the observer itself."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=("chrome", "edge"), default="chrome")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    executable = browser_path(args.browser)
    if executable is None:
        return 2
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "browser").mkdir()
    store = EvidenceStore(directory, [])
    items = scenarios(read_json(ASSETS / "stealth" / "inventory.json"))
    server = ObserverServer(
        store, ASSETS, {"scenarios": [asdict(item) for item in items]}
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = directory / "profile"
    process = subprocess.Popen(
        [
            str(executable),
            "--headless",
            "--no-first-run",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            f"{server.origin}/?browser={args.browser}#{server.token}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=5
        ) as response:
            targets = json.load(response)
        target = next(
            item
            for item in targets
            if item["type"] == "page" and item["url"].startswith(server.origin)
        )
        check = subprocess.run(
            [
                "node",
                str(ASSETS / "stealth" / "resource_smoke.js"),
                target["webSocketDebuggerUrl"],
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        (directory / "result.log").write_text(
            check.stdout + check.stderr, encoding="utf-8"
        )
        print(check.stdout + check.stderr)
        return check.returncode
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
