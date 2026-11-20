"""Execute bounded GUI recipes using real browser and Windows accessibility controls."""

from __future__ import annotations

import importlib
import importlib.util
import time
from pathlib import Path
from typing import Any


def browser_steps(page: Any, steps: list[dict[str, Any]]) -> None:
    """Run accessible browser actions, requiring unambiguous selectors and bounded waits."""
    for step in steps:
        operation = step.get("action", "click")
        if operation == "wait":
            page.wait_for_timeout(min(30000, int(step.get("ms", 500))))
            continue
        locator = (
            page.locator(step["selector"])
            if "selector" in step
            else page.get_by_role(
                step.get("role", "button"), name=step["name"], exact=True
            )
        )
        if operation == "click":
            locator.click(timeout=15000)
        elif operation == "fill":
            locator.fill(str(step["text"]), timeout=15000)
        elif operation == "check":
            locator.set_checked(bool(step["checked"]), timeout=15000)
        elif operation == "expect":
            locator.wait_for(state="visible", timeout=15000)
        else:
            raise ValueError(f"Unsupported browser recipe action {operation}.")


def native_steps(
    steps: list[dict[str, Any]], process: int | None = None
) -> list[dict[str, Any]]:
    """Operate real Windows controls and retain complete picker inventories when requested."""
    desktop = importlib.import_module("pywinauto").Desktop(backend="uia")
    observations = []
    for step in steps:
        selector: dict[str, Any] = {"title_re": step["window"]}
        if process is not None:
            selector["process"] = process
        window = desktop.window(**selector)
        window.wait("visible", timeout=15)
        operation = step.get("action", "click")
        if operation == "inventory":
            controls = window.descendants()
            observations.append(
                {
                    "controls": [
                        {
                            "text": str(control.window_text()),
                            "type": str(control.element_info.control_type),
                            "automation_id": str(control.element_info.automation_id),
                            "rectangle": str(control.rectangle()),
                        }
                        for control in controls
                    ]
                }
            )
            continue
        control = (
            window.child_window(**step.get("control", {}))
            if step.get("control")
            else window
        )
        if operation == "click":
            control.wrapper_object().click_input()
        elif operation == "keys":
            control.type_keys(str(step["keys"]), with_spaces=True, pause=0.05)
        elif operation == "select":
            control.select(str(step["value"]))
        elif operation == "expect":
            control.wait("visible", timeout=15)
        elif operation == "wait":
            time.sleep(min(5, float(step.get("seconds", 0.5))))
        else:
            raise ValueError(f"Unsupported native recipe action {operation}.")
    return observations


def missing_module(name: str) -> str | None:
    """Return an actionable prerequisite without importing a GUI package during preflight."""
    return (
        None
        if importlib.util.find_spec(name) is not None
        else f"Install the test dependency {name}."
    )


def dedicated_profile(value: str) -> Path:
    """Require a marked test profile instead of opening a personal browser profile."""
    path = Path(value).resolve()
    if not (path / ".que-capture-profile").is_file():
        raise ValueError(
            "Create a dedicated profile directory with a .que-capture-profile marker and sign in there first."
        )
    return path
