"""dispatch-envelope-gate: validate delegate_task briefs before dispatch."""

from __future__ import annotations

try:
    from .gate import validate_dispatch
except ImportError:  # pytest imports this plugin directory as a top-level package
    from gate import validate_dispatch

__all__ = ["register"]


def _hook(tool_name="", args=None, **kwargs):
    """Block malformed delegate_task calls before the dispatch tool can run.

    The host treats a pre_tool_call timeout as a block, so this callback only
    performs bounded in-memory string/dict validation. Any unexpected error
    fails open: it must not make delegation impossible.
    """
    try:
        return validate_dispatch(tool_name, args)
    except Exception:
        return None  # fail open: never break dispatch over a gate bug


def register(ctx):
    """Register the synchronous pre-dispatch validator."""
    ctx.register_hook("pre_tool_call", _hook)
