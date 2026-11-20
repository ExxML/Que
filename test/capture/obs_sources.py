"""Print the capture config's OBS "sources" block, read from the running OBS instance."""

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import obsws_python  # type: ignore[import-untyped]
import win32api  # type: ignore[import-untyped]
from run_stealth import browser_path

from capture.adapters import FIXTURE_TITLE, secret

# Matrix capture modes and the OBS method labels that select them.
METHODS = {
    "monitor_capture": {"auto": "Automatic", "dxgi": "DXGI", "wgc": "Windows 10"},
    "window_capture": {"auto": "Automatic", "bitblt": "BitBlt", "wgc": "Windows 10"},
}


def main() -> int:
    """Query OBS's own property lists through a temporary scene, then remove it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4455)
    parser.add_argument("--password-env", default="QUE_CAPTURE_OBS_PASSWORD")
    args = parser.parse_args()
    client = obsws_python.ReqClient(
        host="127.0.0.1",
        port=args.port,
        password=secret(args.password_env),
        timeout=15,
    )
    scene = f"Que-capture-discovery-{uuid.uuid4().hex}"
    client.create_scene(scene)
    try:
        sources: dict[str, dict[str, Any]] = {}
        origins = [
            tuple(win32api.GetMonitorInfo(handle)["Monitor"][:2])
            for handle, _, _ in win32api.EnumDisplayMonitors()
        ]
        browser = browser_path("chrome") or browser_path("edge")
        if browser is None:
            raise RuntimeError(
                "Install Chrome or Edge; it displays the fixture window."
            )
        for kind, source in (
            ("monitor_capture", "monitor"),
            ("window_capture", "window"),
        ):
            name = f"{scene}-{source}"
            # A disabled scene item lets OBS list its properties without capturing.
            client.create_input(scene, name, kind, {}, False)
            methods = items(client, name, "method")
            displays: dict[int, Any] = {}
            if kind == "monitor_capture":
                for label, value in items(client, name, "monitor_id").items():
                    match = re.search(r"@ (-?\d+),(-?\d+)", label)
                    if match and (origin := tuple(map(int, match.groups()))) in origins:
                        displays[origins.index(origin)] = value
            for mode, label in METHODS[kind].items():
                method = next(
                    (value for item, value in methods.items() if label in item), None
                )
                if method is None:
                    print(f"OBS offers no {label} method for {kind}.", file=sys.stderr)
                    continue
                for monitor in range(len(origins)):
                    settings: dict[str, Any] = {"method": method}
                    if kind == "monitor_capture":
                        if monitor not in displays:
                            print(f"OBS lists no display {monitor}.", file=sys.stderr)
                            continue
                        settings["monitor_id"] = displays[monitor]
                    else:
                        # OBS matches "title:class:executable"; the fixture title has no colons.
                        settings["window"] = (
                            f"{FIXTURE_TITLE}:Chrome_WidgetWin_1:{browser.name}"
                        )
                        settings["priority"] = next(
                            value
                            for item, value in items(client, name, "priority").items()
                            if "title" in item.lower()
                        )
                    sources[f"{source}.{mode}.{monitor}"] = {
                        "kind": kind,
                        "settings": settings,
                    }
    finally:
        client.remove_scene(scene)
        client.disconnect()
    print(json.dumps({"sources": sources}, indent=2))
    return 0


def items(client: Any, name: str, prop: str) -> dict[str, Any]:
    """Return an OBS list property's labels and values.

    Args:
        client (Any): The connected OBS request client.
        name (str): The input name.
        prop (str): The list property name.

    Returns:
        dict[str, Any]: The item values keyed by their labels.
    """
    response = client.get_input_properties_list_property_items(name, prop)
    return {
        str(item["itemName"]): item["itemValue"] for item in response.property_items
    }


if __name__ == "__main__":
    sys.exit(main())
