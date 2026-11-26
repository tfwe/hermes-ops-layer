#!/usr/bin/env python3
"""Audit whether long answers to <USER> were drafted inline or delegated.

Measures the parent agent's routing behaviour from state.db instead of asking it
about its own instructions. A prompt about routing measures whether the rule text
exists; this measures what actually happened.

A "violation" is a desktop session containing an assistant message over the word
threshold that was delivered as a final answer (no tool_calls) in a session where
`delegate_task` was never called. Caveat: a session that dispatched somewhere can
still have written its long answer inline, so the second column is an upper bound
on compliance, not proof of it.

Usage:
    writer-route-audit.py                 # last 30 days
    writer-route-audit.py --since 7       # last 7 days
    writer-route-audit.py --since 90 --top 10
    writer-route-audit.py --session <id-prefix>   # one session, message by message
    writer-route-audit.py --threshold 150

Exit code is 0 always: this reports, it does not gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def db_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "state.db"


def word_count(text: str) -> int:
    """Words minus fenced code blocks, which are not authorial prose."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    return len(text.split())


def called_delegate(tool_calls: str | None) -> bool:
    return bool(tool_calls) and "delegate_task" in tool_calls


def scan(since_days: float, threshold: int, source: str) -> list[dict]:
    path = db_path()
    if not path.exists():
        sys.exit(f"no state.db at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cutoff = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    ).timestamp()

    sessions: dict[str, dict] = {}
    turn_dispatched: dict[str, bool] = {}
    rows = conn.execute(
        """SELECT m.session_id, m.role, m.content, m.tool_calls, s.source
           FROM messages m JOIN sessions s ON s.id = m.session_id
           WHERE m.role IN ('user','assistant') AND m.timestamp > ?
             AND COALESCE(m.active, 1) = 1
             AND COALESCE(m.compacted, 0) = 0
             AND (m.display_kind IS NULL OR m.display_kind NOT IN
                  ('hidden','internal_notification','process_complete',
                   'async_delegation_complete','steer','failed_turn'))
           ORDER BY m.session_id, m.id""",
        (cutoff,),
    )
    for row in rows:
        sid = row["session_id"]
        if row["role"] == "user":
            turn_dispatched[sid] = False  # new turn: the flag is per-turn
            sessions.setdefault(
                sid,
                {
                    "source": row["source"],
                    "max_words": 0,
                    "long": 0,
                    "dispatches": 0,
                    "inline_long": 0,
                },
            )
            continue
        rec = sessions.setdefault(
            sid,
            {
                "source": row["source"],
                "max_words": 0,
                "long": 0,
                "dispatches": 0,
                "inline_long": 0,
            },
        )
        if called_delegate(row["tool_calls"]):
            rec["dispatches"] += 1
            turn_dispatched[sid] = True
        if row["content"] and not row["tool_calls"]:
            words = word_count(row["content"])
            rec["max_words"] = max(rec["max_words"], words)
            if words > threshold:
                rec["long"] += 1
                # Turn-scoped: a dispatch earlier in this same turn covers the answer.
                if not turn_dispatched.get(sid):
                    rec["inline_long"] += 1
    conn.close()
    return [
        dict(session=sid, **rec)
        for sid, rec in sessions.items()
        if source == "all" or rec["source"] == source
    ]


def report(recs: list[dict], threshold: int, top: int) -> int:
    long_total = sum(r["long"] for r in recs)
    inline = sum(r["inline_long"] for r in recs)
    long_sessions = [r for r in recs if r["long"]]

    print(f"sessions scanned: {len(recs)}")
    if not long_total:
        print(f"no answer over {threshold} words; nothing to route")
        return 0
    pct = 100.0 * inline / long_total
    print(
        f"answers over {threshold}w: {long_total} across {len(long_sessions)} sessions "
        f"— {inline} written inline with no dispatch in that turn ({pct:.0f}%), "
        f"{long_total - inline} dispatched within the turn"
    )
    print(f"no long answer: {len(recs) - len(long_sessions)}")
    worst = sorted([r for r in recs if r["inline_long"]], key=lambda x: -x["inline_long"])[:top]
    if worst:
        print("worst offenders (by inline long answers):")
        for r in worst:
            print(
                f"  {r['session'][:26]}  inline_long={r['inline_long']}"
                f"  max={r['max_words']}w  source={r['source']}"
            )
    return 0


def one_session(prefix: str, threshold: int) -> int:
    path = db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ORDER BY id DESC", (prefix + "%",)
        )
    ]
    if not ids:
        sys.exit(f"no session starting with {prefix!r}")
    sid = ids[0]
    print(f"session {sid}")
    for row in conn.execute(
        "SELECT role, content, tool_calls, tool_name FROM messages"
        " WHERE session_id = ? ORDER BY id",
        (sid,),
    ):
        if row["role"] == "assistant":
            if row["tool_calls"]:
                names = [
                    tc.get("function", {}).get("name", "?")
                    for tc in json.loads(row["tool_calls"] or "[]")
                ]
                print(f"  tool  {'+'.join(names)}")
            elif row["content"]:
                words = word_count(row["content"])
                flag = "  <-- LONG, INLINE" if words > threshold else ""
                print(f"  answer {words}w{flag}")
        elif row["role"] == "user":
            print(f"  user  {(row['content'] or '')[:60]!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", type=float, default=30, help="days back (default 30)")
    ap.add_argument("--threshold", type=int, default=200, help="word threshold")
    ap.add_argument("--top", type=int, default=6, help="offenders to list")
    ap.add_argument("--source", default="desktop", help="desktop|telegram|all")
    ap.add_argument("--session", help="inspect one session id prefix instead")
    args = ap.parse_args()
    if args.session:
        return one_session(args.session, args.threshold)
    return report(scan(args.since, args.threshold, args.source), args.threshold, args.top)


if __name__ == "__main__":
    raise SystemExit(main())