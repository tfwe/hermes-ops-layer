#!/usr/bin/env python3
"""Weekly writer-route report: volume, and whether the prompt cache engaged.

Context: on 18 Nov 2026 the writer route moved from anthropic/claude-haiku-5-5 to
anthropic/claude-sonnet-5-5, and prompt_caching was declared per model for
anthropic/claude-sonnet-5-5 and anthropic/claude-haiku-5-5 on the
hopscotchlabs-ai-completions provider.

Two questions:
  1. Has prompt_caching actually taken effect? (cache_read/cache_write were 0
     across every call before the declaration)
  2. What is the volume, to size the tier decision?

NOTE ON VISIBILITY: the writer and reviewer routes now share one model
(claude-sonnet-5-5), and session_model_usage keys on model only - the `task`
column is empty on these rows - so their counters cannot be separated. The
"writer+reviewer" line below is a pooled figure, not a writer-only number.

Prints a compact report to stdout. Zero tokens, stdlib only, read-only.
Exit 0 always - the watchdog owns hard failures, this job owns reporting.
"""
import os
import sqlite3
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, ".hermes", "state.db")
WINDOW_DAYS = 7

# label, model id, must-show-cache
ROUTES = [
    ("writer+reviewer sonnet-5-5", "anthropic/claude-sonnet-5-5", True),
    ("coder    gpt-6-luna", "openai-main/gpt-6-luna", False),
    ("parent   ds-v4.1-flash", "deepseek/deepseek-v4.1-flash", False),
]
# retired routes, kept visible until their old rows age out of the window
HISTORY = [
    ("[retired] writer haiku-5-5", "anthropic/claude-haiku-5-5"),
]


def main():
    if not os.path.exists(DB):
        print(f"writer-route report: state.db missing at {DB}")
        return 0
    cutoff = datetime.now().timestamp() - WINDOW_DAYS * 86400
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = list(con.execute(
            "select model, sum(api_call_count), sum(input_tokens), sum(output_tokens),"
            " sum(cache_read_tokens), sum(cache_write_tokens)"
            " from session_model_usage where last_seen >= ? group by model", (cutoff,)))
    except sqlite3.Error as exc:
        print(f"writer-route report: query failed: {exc}")
        return 0
    by_model = {r[0]: r[1:] for r in rows}

    print(f"Writer-route report - last {WINDOW_DAYS} days")
    print(f"{'route':28}{'calls':>7}{'in':>12}{'out':>10}{'cache_r':>12}{'cache_w':>10}")
    flags = []
    for label, model, must_cache in ROUTES:
        r = by_model.get(model)
        if not r:
            print(f"{label:28}{'-':>7}{'-':>12}{'-':>10}{'-':>12}{'-':>10}")
            continue
        calls, tin, tout, cr, cw = (x or 0 for x in r)
        print(f"{label:28}{calls:>7}{tin:>12}{tout:>10}{cr:>12}{cw:>10}")
        if must_cache and calls and not (cr or cw):
            flags.append(f"{model}: {calls} calls, zero cache activity - prompt_caching not engaging")

    for label, model in HISTORY:
        r = by_model.get(model)
        if r:
            calls, tin, tout, cr, cw = (x or 0 for x in r)
            print(f"{label:28}{calls:>7}{tin:>12}{tout:>10}{cr:>12}{cw:>10}")
            # Informational only: these rows are pre-switch calls still inside the
            # window, so a non-zero count here is expected for days after the move.
            # A count that KEEPS GROWING across weeks means the route was reverted.

    if flags:
        print("\nATTENTION")
        for f in flags:
            print(f"  - {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
