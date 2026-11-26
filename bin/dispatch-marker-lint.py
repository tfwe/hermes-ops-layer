#!/usr/bin/env python3
"""Pre-flight check for delegate_task goals: list unexpanded template markers.

The core tool refuses any dispatch whose task `goal` contains a marker shaped
`<word-word>` / `{word word}` (tools/delegate_tool_tasks.py::_TEMPLATE_MARKER_RE).
This lints the payload BEFORE the call so the refusal never costs a turn, and it
uses the core regex itself when importable, so the two cannot drift.

Usage:
    dispatch-marker-lint.py '<goal text>'
    dispatch-marker-lint.py payload.json          # {"tasks": [...]} or a tasks list
    echo '<goal text>' | dispatch-marker-lint.py -

Exit 0 = clean, 1 = markers found, 2 = usage/parse error.
Nothing is printed when clean, so a silent run means go.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FALLBACK = re.compile(
    r"<[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)+>|\{[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)+\}"
)


def marker_re() -> re.Pattern[str]:
    core = Path.home() / ".hermes" / "hermes-agent" / "tools" / "delegate_tool_tasks.py"
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_delegate_tasks_for_lint", core)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._TEMPLATE_MARKER_RE
    except Exception:  # noqa: BLE001 - fails openly on drift, never crashes
        return FALLBACK


def goals_of(obj) -> list[tuple[str, str]]:
    """(label, goal) pairs from a payload dict/list or a bare string."""
    if isinstance(obj, str):
        return [("goal", obj)]
    if isinstance(obj, list):
        tasks = obj
    elif isinstance(obj, dict):
        tasks = obj.get("tasks") or []
    else:
        return []
    out = []
    for i, task in enumerate(tasks):
        if isinstance(task, dict):
            out.append((f"tasks[{i}].goal", str(task.get("goal", ""))))
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    raw = sys.stdin.read() if argv[0] == "-" else (" ".join(argv))

    candidates = []
    stripped = raw.strip()
    if stripped[:1] in "[{":
        try:
            candidates = goals_of(json.loads(stripped))
        except json.JSONDecodeError:
            candidates = []
    if not candidates:
        candidates = [("goal", raw)]

    rx = marker_re()
    hits = []
    for label, goal in candidates:
        for m in rx.finditer(goal):
            start = max(0, m.start() - 45)
            snippet = goal[start:m.end() + 45].replace("\n", " ")
            hits.append(f"{label}: {m.group(0)} … {snippet}")

    for line in hits:
        print(f"MARKER {line}")
    if hits:
        print(
            f"\n{len(hits)} unexpanded marker(s). Substitute real values (a timestamp from "
            "`date -u +%Y%m%dT%H%MZ`), describe a format in words, or move a literal template "
            "placeholder into `context` — only `goal` is scanned."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))