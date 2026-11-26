"""Pre-write check for memory writes, backed by hermes-claim-gate.py --text.

Only the memory tool is inspected. Skill bodies are deliberately out of scope: a
skill legitimately names the route it tells the agent to use, so treating that as
a violation would block ordinary skill edits. Skill-side duplication is reported
by hermes-claim-gate.py --report and hermes-skill-refs.py instead, as a backlog.
Each proposed memory text is piped to ``hermes-claim-gate.py --text``. Exit 1 with violation lines is
the only thing that can block. Exit 2, a timeout, a missing script or any exception is
logged and allowed (fail open).

Dry-run is the default. A block happens only when CLAIM_GATE_ENFORCE == "1".
Every decision made in dry-run is logged, so the log shows what enforcement would have
blocked. In enforce mode, only blocks and errors are logged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

WATCHED_TOOLS = frozenset({"memory"})
MEMORY_WRITE_ACTIONS = frozenset({"add", "patch"})
TIMEOUT_SECS = 5  # host hook callbacks time out at 30s; stay well inside it


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _script() -> str:
    return os.environ.get("CLAIM_GATE_SCRIPT") or os.path.join(
        os.path.expanduser("~"), "bin", "hermes-claim-gate.py"
    )


def _log_path() -> str:
    return os.environ.get("CLAIM_GATE_LOG") or os.path.join(_hermes_home(), "logs", "claim-gate.jsonl")


def enforcing() -> bool:
    """Armed by a flag file, so arming needs no unit edit and no restart of a service.

    An env var only works for a process started after someone exported it, which for a
    systemd-run backend means editing the unit; a file is read on every call.
    """
    if os.environ.get("CLAIM_GATE_ENFORCE") == "1":
        return True
    return os.path.exists(os.environ.get("CLAIM_GATE_FLAG") or os.path.join(
        _hermes_home(), "state", "claim-gate-enforce"))


def _ops(args: dict[str, Any]) -> list[dict[str, Any]]:
    ops = args.get("operations")
    if isinstance(ops, list):
        return [op for op in ops if isinstance(op, dict)]
    return [args]  # legacy flat single-op shape


def _text_of(value: Any) -> str:
    return value if isinstance(value, str) else ""


def extract(tool_name: str, args: Any) -> list[tuple[str, str]]:
    """Return (label, proposed_text) pairs for the writes in this call. Empty when not watched."""
    if not isinstance(args, dict):
        return []
    if tool_name == "memory":
        target = "user" if args.get("target") == "user" else "memory"
        label = "memories/USER.md" if target == "user" else "memories/MEMORY.md"
        parts = []
        for op in _ops(args):
            if op.get("action") not in MEMORY_WRITE_ACTIONS:
                continue  # 'remove' writes no new text
            for key in ("content", "new_text"):
                parts.append(_text_of(op.get(key)))
        text = "\n".join(p for p in parts if p)
        return [(label, text)] if text else []
    return []


def _log(record: dict[str, Any]) -> None:
    """Append one JSON line. Never raises: logging is not worth a broken tool call."""
    try:
        record = {"ts": int(time.time() * 1000), **record}
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def run_check(label: str, text: str) -> tuple[str, list[str], str]:
    """Return (verdict, violation_lines, detail). verdict: clean | violation | error."""
    try:
        proc = subprocess.run(
            [sys.executable, _script(), "--text", "--as", label],
            input=text,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
        )
    except Exception as exc:  # missing script, timeout, OS error
        return "error", [], f"{type(exc).__name__}: {exc}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode == 0:
        return "clean", [], ""
    if proc.returncode == 1 and lines:
        return "violation", lines, ""
    return "error", [], f"exit {proc.returncode}: {proc.stderr.strip()[:300] or 'no output'}"


def _block_message(violations: list[str]) -> str:
    return (
        "Claim gate blocked this write: the text restates a fact that belongs to another file.\n"
        + "\n".join(violations)
        + "\nRemove the route name, model id, or escalation policy from the text and retry. "
        "Route and model facts belong only in config.yaml; the escalation ladder belongs only in SOUL.md."
    )


def handle(tool_name: str, args: Any) -> dict[str, str] | None:
    """Decide one call. Returns a block directive or None. Callers must wrap this in try/except."""
    if tool_name not in WATCHED_TOOLS:
        return None
    checks = extract(tool_name, args)
    if not checks:
        return None
    enforce = enforcing()
    mode = "enforce" if enforce else "dry-run"
    all_violations: list[str] = []
    for label, text in checks:
        verdict, violations, detail = run_check(label, text)
        if verdict == "error":
            _log({"tool": tool_name, "label": label, "mode": mode, "verdict": "error",
                  "action": "allow", "detail": detail})
            continue
        if verdict == "violation":
            all_violations.extend(violations)
            continue
        if not enforce:
            _log({"tool": tool_name, "label": label, "mode": mode, "verdict": "clean", "action": "allow"})

    if not all_violations:
        return None
    if not enforce:
        _log({"tool": tool_name, "mode": mode, "verdict": "violation", "action": "allow",
              "would_block": True, "violations": all_violations})
        return None
    _log({"tool": tool_name, "mode": mode, "verdict": "violation", "action": "block",
          "violations": all_violations})
    return {"action": "block", "message": _block_message(all_violations)}
