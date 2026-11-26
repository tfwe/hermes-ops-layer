"""claim-gate-hook: pre-write check of memory and skill text via hermes-claim-gate.py --text.

Dry-run by default. Set CLAIM_GATE_ENFORCE=1 in the environment to block real violations.
"""

from __future__ import annotations

try:
    from .gate import handle
except ImportError:  # loaded as a top-level module (e.g. by the test script)
    from gate import handle

__all__ = ["register"]


def _hook(tool_name="", args=None, **kwargs):
    """Inspect memory and skill_manage writes before they run.

    Fails open: any exception returns None, so the call proceeds. A gate bug must not
    block the agent from writing memory.
    """
    try:
        return handle(tool_name, args)
    except Exception:
        return None


def register(ctx):
    """Register the pre-write check on pre_tool_call."""
    ctx.register_hook("pre_tool_call", _hook)
