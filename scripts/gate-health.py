#!/usr/bin/env python3
"""Prove the dispatch-envelope-gate is enabled AND actually refusing bad briefs.

The failure this exists for: the gate was silently moved to plugins.disabled and
every malformed dispatch went through for hours. "Installed" is not "enforcing".
This checks three things and exits non-zero on any failure:

  1. config.yaml lists dispatch-envelope-gate under plugins.enabled and not disabled
  2. a dispatch with no envelope is BLOCKED
  3. a writer dispatch whose goal carries the six sections and real facts PASSES

Silent when healthy (exit 0, no output). Zero tokens, no network.
"""

from __future__ import annotations

import json
import os
import sys

HOME = os.path.expanduser("~/.hermes")
CONFIG = os.path.join(HOME, "config.yaml")
PLUGIN = os.path.join(HOME, "plugins", "dispatch-envelope-gate")

BRIEF = (
    "TASK: draft the reply\n"
    "FACTS: Neon costs $3/$15 per M; AA score 59.\n"
    "PATHS: n/a\n"
    "OUTPUT: message only\n"
    "CHECK: reads as human\n"
    "CEILING: 6 turns"
)


def fail(msg: str) -> "NoReturn":
    print(f"gate-health: {msg}")
    sys.exit(1)


def main() -> None:
    try:
        import yaml
    except Exception:
        fail("pyyaml unavailable; cannot read config")
    try:
        cfg = yaml.safe_load(open(CONFIG, encoding="utf-8")) or {}
    except Exception as exc:
        fail(f"cannot read {CONFIG}: {exc}")

    plugins = cfg.get("plugins") or {}
    enabled = set(plugins.get("enabled") or [])
    disabled = set(plugins.get("disabled") or [])
    if "dispatch-envelope-gate" in disabled:
        fail("dispatch-envelope-gate is in plugins.disabled; every bad brief passes")
    if "dispatch-envelope-gate" not in enabled:
        fail("dispatch-envelope-gate is not in plugins.enabled; the gate is not loaded")

    sys.path.insert(0, PLUGIN)
    try:
        from gate import validate_dispatch
    except Exception as exc:
        fail(f"gate module will not import: {exc}")

    bare = {"tasks": [{"goal": "Reply with the single word OK and nothing else.", "option": "coder"}]}
    if validate_dispatch("delegate_task", bare) is None:
        fail("an envelope-less dispatch was ACCEPTED; the gate is not enforcing")

    good = {"tasks": [{"goal": BRIEF, "option": "writer"}]}
    if validate_dispatch("delegate_task", good) is not None:
        fail("a valid goal-only writer brief was REFUSED; the gate over-blocks")

    # Enabled, blocking the bad, accepting the good. Nothing to say.
    sys.exit(0)


if __name__ == "__main__":
    main()
