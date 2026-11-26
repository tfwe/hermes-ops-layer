#!/usr/bin/env python3
"""Offline self-test for dispatch-envelope-gate.

Exercises the registered hook and representative pass/block/fail-open paths
without invoking Hermes, starting a child, or making any network calls.
Exit 0 means healthy; exit 1 means one or more checks failed.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

PLUGIN = Path(os.environ.get("HERMES_PLUGINS_DIR", Path.home() / ".hermes" / "plugins")) / "dispatch-envelope-gate"

GOOD_CONTEXT = """TASK: implement the requested behavior
FACTS: Existing API behavior must remain compatible
PATHS: /repo/src/module.py and /repo/tests/test_module.py
OUTPUT: Return a summary and changed paths
CHECK: Run the focused test suite and report its result
CEILING: at most 4 turns, then stop and report
"""


def load_gate():
    spec = importlib.util.spec_from_file_location("dispatch_envelope_gate_selftest", PLUGIN / "gate.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    failures = []

    def check(label, condition):
        if condition:
            print(f"PASS {label}")
        else:
            print(f"FAIL {label}")
            failures.append(label)

    required = ("plugin.yaml", "__init__.py", "gate.py", "tests/test_gate.py", "pytest.ini")
    for relative in required:
        check(f"plugin file exists: {relative}", (PLUGIN / relative).is_file())

    try:
        gate = load_gate()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL gate import: {type(exc).__name__}: {exc}")
        return 1
    print("PASS gate import")

    good = {"goal": "Implement the validation behavior and tests", "option": "coder", "context": GOOD_CONTEXT}
    check("conformant task passes", gate.validate_dispatch("delegate_task", {"tasks": [good]}) is None)

    malformed = gate.validate_dispatch("delegate_task", {"tasks": [{"goal": "placeholder"}]})
    check("placeholder and missing context are blocked", isinstance(malformed, dict) and malformed.get("action") == "block" and bool(malformed.get("message")))

    missing_context = gate.validate_dispatch("delegate_task", {"tasks": [{"goal": "Write a clear integration guide", "option": "writer"}]})
    check("proper goal without context is blocked", isinstance(missing_context, dict) and missing_context.get("action") == "block")

    mixed = gate.validate_dispatch("delegate_task", {"tasks": [good, {"goal": "placeholder", "option": "writer"}]})
    check("mixed batch identifies task #2", isinstance(mixed, dict) and "#2" in mixed.get("message", ""))

    steer = gate.validate_dispatch("delegate_task", {"action": "steer", "message": "TASK: primary task\nCEILING: 3 turns"})
    check("misused primary brief in steer is blocked", isinstance(steer, dict) and "next tool result" in steer.get("message", ""))

    malformed_args = [gate.validate_dispatch("delegate_task", args) for args in (None, "bad", [])]
    check("malformed args fail safely", all(isinstance(item, dict) and item.get("action") == "block" and bool(item.get("message")) for item in malformed_args))

    class Recorder:
        def __init__(self):
            self.hooks = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    recorder = Recorder()
    try:
        import importlib.util
        import sys
        package_name = "dispatch_envelope_gate"
        package_spec = importlib.util.spec_from_file_location(
            package_name,
            PLUGIN / "__init__.py",
            submodule_search_locations=[str(PLUGIN)],
        )
        if package_spec is None or package_spec.loader is None:
            raise RuntimeError("could not load plugin package")
        package = importlib.util.module_from_spec(package_spec)
        sys.modules[package_name] = package
        package_spec.loader.exec_module(package)
        package.register(recorder)
        check("register attaches pre_tool_call", "pre_tool_call" in recorder.hooks)
        check("registered hook allows valid batch", recorder.hooks["pre_tool_call"](tool_name="delegate_task", args={"tasks": [good]}) is None)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL hook registration: {type(exc).__name__}: {exc}")
        failures.append("hook registration")

    try:
        package_module = sys.modules["dispatch_envelope_gate.gate"]
        saved = package_module.validate_dispatch
        package_module.validate_dispatch = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe"))
        try:
            result = sys.modules["dispatch_envelope_gate"]._hook(tool_name="delegate_task", args={"tasks": [good]})
        finally:
            package_module.validate_dispatch = saved
        check("unexpected hook error fails open", result is None)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL fail-open probe: {type(exc).__name__}: {exc}")
        failures.append("unexpected hook error fails open")

    if failures:
        print(f"dispatch-gate selftest FAILED: {len(failures)} check(s): " + "; ".join(failures))
        return 1
    print("dispatch-gate selftest PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
