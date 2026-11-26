"""writer-route-gate: deterministic relay check on transform_llm_output.

Two deterministic checks run on each reply: the relay check (may append a notice or
replace the text, via gate.decide_ex) and the scanning-deliverable lint (advisory only:
hermes-lint-reply.py findings are logged to writer-gate.jsonl, never rewritten in).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from .gate import _config, _words, carried_fraction, decide, decide_ex, log_decision, turn_facts

__all__ = ["register"]

_LINT_TOOL = Path.home() / "bin" / "hermes-lint-reply.py"
_LINT_MODULE = None


def _lint_module():
    global _LINT_MODULE
    if _LINT_MODULE is None:
        spec = importlib.util.spec_from_file_location("hermes_lint_reply", _LINT_TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LINT_MODULE = module
    return _LINT_MODULE


def _lint_long_reply(response_text: str, session_id: str) -> None:
    """Lint a reply past the relay word floor and log the findings. Never raises."""
    try:
        if not response_text or _words(response_text) <= int(_config()["word_floor"]):
            return
        findings, words = _lint_module().lint(response_text)
        log_decision(
            session_id,
            "lint_reply",
            {
                "words": words,
                "count": len(findings),
                "findings": [f"{sev} L{line}: {msg}" for sev, line, msg in findings][:20],
            },
        )
    except Exception:
        return  # fail open: a lint fault never costs the reply


def _hook(response_text="", session_id="", model="", platform="", turn_id="", **kwargs):
    _lint_long_reply(response_text, session_id)
    try:
        _, replacement = decide_ex(response_text, session_id, auditor=True)
        return replacement
    except Exception:
        return None  # fail open: never break a turn over this



def register(ctx):
    ctx.register_hook("transform_llm_output", _hook)