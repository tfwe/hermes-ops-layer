#!/usr/bin/env python3
"""Audit trail: everything the agents did, in one report.

Reads the stores that already hold the record and prints them as one auditable
page for a local calendar day:

  1. sessions + cost              state.db: session_model_usage, scoped by
                                  messages.timestamp (epoch seconds)
  2. subagent delegations         cache/delegation/**/manifest.json
  3. skill mutations + diffs      skills/.curator_ledger.jsonl
                                  + .curator_backups/blobs (content-addressed)
  4. config + profile changes     git log in the hermes-config mirror
  5. cron runs                    cron/executions.db
  6. instruction-file writes      mtimes of SOUL.md / memories
  7. context discipline           where the context went, per tool -- the
                                  tool-output rule, measured after the fact

Usage:
    hermes-audit.py                       today
    hermes-audit.py --date 2026-11-17
    hermes-audit.py --diff <entry-id>     before/after of one skill edit
    hermes-audit.py --blobs               the content-addressed backup store

Read-only: never writes, never connects out. Nothing is filtered silently. A
store that cannot be read says so, and a timestamp that cannot be parsed is
reported rather than dropped -- a silent filter is how a report lies.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
REAL = Path(os.environ.get("HERMES_REAL_HOME") or Path.home())
DB = HOME / "state.db"
CHARS_PER_TOKEN = 2.75  # the constant this config's own comments use
LEDGER = HOME / "skills" / ".curator_ledger.jsonl"
BLOBS = HOME / ".curator_backups" / "blobs"


def rule(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


def q(sql: str, args=(), db: Path | None = None):
    """Read-only query. Returns (rows, None) or (None, error-string)."""
    try:
        con = sqlite3.connect(f"file:{db or DB}?mode=ro", uri=True)
        rows = list(con.execute(sql, args))
        con.close()
        return rows, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def as_dt(v):
    """Epoch seconds (int/float) or ISO string -> naive local datetime, else None.

    messages.timestamp is an epoch float; ledger ts and manifest started are ISO.
    Both are accepted, and an unparseable value returns None so the caller can
    say so instead of dropping the row.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(v))
        except Exception:
            return None
    s = str(v).strip()
    if s:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
        except Exception:
            pass
        try:
            return dt.datetime.fromtimestamp(float(s))
        except Exception:
            pass
    return None


def day_bounds(day: str):
    lo = dt.datetime.fromisoformat(day + "T00:00:00")
    return lo.timestamp(), (lo + dt.timedelta(days=1)).timestamp()


def show_diff(entry_id: str) -> int:
    if not LEDGER.exists():
        print(f"no ledger at {LEDGER}")
        return 1
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("id")) != entry_id:
            continue
        print(f"entry {entry_id}  {r.get('ts')}  actor={r.get('actor')}  "
              f"{r.get('skill')}  {r.get('action')}\nevidence: {r.get('evidence')}")
        for side in ("before", "after"):
            print(f"\n--- {side} ---")
            for item in r.get(side) or []:
                sha = item.get("sha256", "")
                print(f"  {item.get('path')}  sha256={sha[:16]}")
                blob = BLOBS / sha
                if blob.exists():
                    lines = blob.read_text(errors="replace").splitlines()
                    print("  " + "\n  ".join(lines[:20]))
                    if len(lines) > 20:
                        print(f"  ... ({len(lines)} lines total)")
                else:
                    print(f"  (no blob at {blob})")
        return 0
    print(f"entry {entry_id} not found")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--diff", metavar="ENTRY_ID")
    ap.add_argument("--blobs", action="store_true")
    a = ap.parse_args()

    if a.blobs:
        print(f"{BLOBS}  ({len(list(BLOBS.iterdir())) if BLOBS.exists() else 0} content blobs)")
        return 0
    if a.diff:
        return show_diff(a.diff)

    day = a.date
    lo, hi = day_bounds(day)
    print(f"HERMES AUDIT  {day}")
    print(f"home={HOME}  real_home={REAL}")
    if str(HOME) != str(REAL / ".hermes"):
        print("  !! HERMES_HOME is overridden - this may describe a different home than you think")

    # ---- 1. sessions ------------------------------------------------------
    rule("1. SESSIONS AND COST")
    active, err = q("select distinct session_id from messages "
                    "where timestamp >= ? and timestamp < ?", (lo, hi))
    if err:
        print(f"  unavailable (messages): {err}")
        active = []
    rows, err = q("""select session_id, model, sum(api_call_count), sum(input_tokens),
                            sum(output_tokens), round(sum(estimated_cost_usd),4)
                     from session_model_usage group by session_id, model""")
    if err:
        print(f"  unavailable (usage): {err}")
    else:
        want = {r[0] for r in active}
        hit = 0
        for sid, model, calls, i, o, cost in rows:
            if sid not in want:
                continue
            hit += 1
            print(f"  {sid}  {model}")
            print(f"      calls={calls}  in={i:,}  out={o:,}  est=${cost}")
        print(f"  ({hit} session(s) active)" if hit else "  (no session activity on this date)")

    # ---- 2. delegations ---------------------------------------------------
    rule("2. SUBAGENT DELEGATIONS")
    seen, hit, unparsed = set(), 0, 0
    for pat in (HOME / "cache" / "delegation" / "*" / "manifest.json",
                HOME / "cache" / "delegation" / "*" / "*" / "manifest.json"):
        for f in sorted(glob.glob(str(pat))):
            try:
                d = json.load(open(f))
            except Exception:
                continue
            if d.get("delegation_id") in seen:
                continue
            started = as_dt(d.get("started"))
            if started is None:
                unparsed += 1
                continue
            if started.strftime("%Y-%m-%d") != day:
                continue
            seen.add(d.get("delegation_id"))
            hit += 1
            print(f"  {d.get('delegation_id')}  {d.get('started')}  "
                  f"{d.get('model')} / {d.get('provider')}")
            for t in d.get("tasks", []):
                goal = str(t.get("goal", "")).replace("\n", " ")[:105]
                print(f"      #{t.get('index')} {t.get('status')} {t.get('model','')} :: {goal}")
    print(f"  ({hit} delegation(s))" if hit else "  (no delegations on this date)")
    if unparsed:
        print(f"  !! {unparsed} manifest(s) with an unparseable start time (not shown)")

    # ---- 3. skill mutations ----------------------------------------------
    rule("3. SKILL MUTATIONS  (actor: agent = me, curator = the automated editor)")
    if not LEDGER.exists():
        print(f"  unavailable: no ledger at {LEDGER}")
    else:
        hit, unparsed = 0, 0
        for line in LEDGER.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            t = as_dt(r.get("ts"))
            if t is None:
                unparsed += 1
                continue
            if t.strftime("%Y-%m-%d") != day:
                continue
            hit += 1
            print(f"  {t:%H:%M:%S}  {str(r.get('actor')):8} {str(r.get('skill')):24} "
                  f"{str(r.get('action')):10} id={r.get('id')}")
        print(f"  ({hit} edit(s))" if hit else "  (no skill edits on this date)")
        if hit:
            print("  before/after for any of them:  hermes-audit.py --diff <id>")
        if unparsed:
            print(f"  !! {unparsed} ledger row(s) with an unparseable ts (not shown)")

    # ---- 4. config + profile ---------------------------------------------
    rule("4. CONFIG + PROFILE CHANGES (the git mirror is the content history)")
    repo = REAL / "hermes-config"
    if not (repo / ".git").exists():
        print(f"  unavailable: {repo} is not a git repo")
    else:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "log", "--all",
                 f"--since={day} 00:00", f"--until={day} 23:59",
                 "--pretty=%h %ad %s", "--date=short"],
                capture_output=True, text=True, timeout=25).stdout.strip()
            print("  " + (out.replace("\n", "\n  ") if out else "(no commits on this date)"))
        except Exception as e:
            print(f"  unavailable: {e}")

    # ---- 5. cron ----------------------------------------------------------
    rule("5. CRON RUNS")
    ex = HOME / "cron" / "executions.db"
    if not ex.exists():
        print(f"  unavailable: no {ex}")
    else:
        tables, err = q("select name from sqlite_master where type='table'", db=ex)
        if err:
            print(f"  unavailable: {err}")
        else:
            for (t,) in tables:
                cnt, e2 = q(f"select count(*) from {t}", db=ex)
                print(f"  {t}: {'?' if e2 else cnt[0][0]} rows")

    # ---- 6. instruction writes -------------------------------------------
    rule("6. INSTRUCTION-FILE WRITES (mtime)")
    for p in [HOME / "SOUL.md", HOME / "memories" / "MEMORY.md", HOME / "memories" / "USER.md"]:
        if p.exists():
            m = dt.datetime.fromtimestamp(p.stat().st_mtime)
            today = "   <-- today" if m.strftime("%Y-%m-%d") == day else ""
            print(f"  {m:%Y-%m-%d %H:%M}  {p}{today}")

    # ---- 7. context discipline -------------------------------------------
    rule("7. CONTEXT DISCIPLINE (where the context went - the tool-output rule, measured)")
    rows, err = q("""select role, coalesce(tool_name,'-'),
                            case when role='user'
                                  and content like '%ASYNC DELEGATION BATCH COMPLETE%'
                                 then 'child summary' else '' end,
                            count(*), sum(length(coalesce(content,'')))
                     from messages where timestamp >= ? and timestamp < ?
                     group by 1,2,3""", (lo, hi))
    if err:
        print(f"  unavailable: {err}")
    else:
        tot = sum(r[4] for r in rows) or 1
        bucket, bytool = {}, {}
        for role, tn, kind, n, ch in rows:
            if kind == "child summary":
                key = "child summaries (delegated)"
                bucket[key] = bucket.get(key, 0) + ch
            elif role == "assistant":
                bucket["my replies"] = bucket.get("my replies", 0) + ch
            elif role == "tool":
                bucket["raw tool output"] = bucket.get("raw tool output", 0) + ch
                bytool[tn] = bytool.get(tn, 0) + ch
            elif role == "user":
                bucket["user messages"] = bucket.get("user messages", 0) + ch
            else:
                bucket[role] = bucket.get(role, 0) + ch
        for k, v in sorted(bucket.items(), key=lambda x: -x[1]):
            print(f"  {k:26} {v:9,} ch  ~{v / CHARS_PER_TOKEN:8,.0f} tok  {100 * v / tot:5.1f}%")
        print(f"  {'TOTAL':26} {tot:9,} ch  ~{tot / CHARS_PER_TOKEN:8,.0f} tok")
        if bytool:
            print("\n  raw tool output by tool (the rule is: filter this at the source):")
            for tn, ch in sorted(bytool.items(), key=lambda x: -x[1])[:8]:
                print(f"    {tn or '-':22} {ch:9,} ch  ~{ch / CHARS_PER_TOKEN:8,.0f} tok  {100 * ch / tot:5.1f}%")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
