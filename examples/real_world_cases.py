#!/usr/bin/env python3
"""Real-world failure-case suite: incidents the guards exist for, replayed.

Every case here is modeled on a real failure pattern the layer has actually
hit — an unexpanded template marker refused by delegate_task, a process kept
green by heartbeats while its data went stale, a skill body naming a script
that exists but cannot run, a reply that reads as machine-written. Each case
asserts that the real guard catches it (and that a healthy control passes),
and prints the guard's actual output. Zero tokens, stdlib only, offline.

Run: python3 examples/real_world_cases.py   exit 0 = every case caught.

The distinction from bin/*.test.sh fixtures: those prove the guards decide;
these prove the guards catch realistic incidents.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
failures: list[str] = []


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case(title: str):
    def deco(fn):
        def run():
            name, ok, detail = fn()
            print(f"{'CAUGHT' if ok else 'MISSED'}  {name}")
            for line in detail:
                print(f"    {line}")
            if not ok:
                failures.append(name)
        run.__name__ = fn.__name__
        return run
    return deco


# --- 1. unexpanded template marker in a delegate_task goal -------------------
# Real incident (verbatim line lives in scripts/check_delegate_failures.py):
#   Tool delegate_task returned error: Task 0 goal contains an unexpanded
#   template marker ('<UTC-ts>'). The marker-lint pre-flight must catch the
#   same shape before it is ever dispatched.

GOAL_TEMPLATE = ("Report on the <UTC-ts> deploy window and list the failing "
                 "services since the last changeover.")
GOAL_REAL = GOAL_TEMPLATE.replace("<UTC-ts>", "20261121T1651Z")


@case("unexpanded template marker in dispatch goal")
def _marker_bad():
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad-goal.txt"
        bad.write_text(GOAL_TEMPLATE + "\n")
        p = subprocess.run([sys.executable, str(REPO / "bin" / "dispatch-marker-lint.py"),
                            GOAL_TEMPLATE], capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 1 and "MARKER" in out and "<UTC-ts>" in out
        return "unexpanded '<UTC-ts>' in a dispatch goal", ok, out.splitlines()


@case("expanded goal passes the marker lint (control)")
def _marker_good():
    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "good-goal.txt"
        good.write_text(GOAL_REAL + "\n")
        p = subprocess.run([sys.executable, str(REPO / "bin" / "dispatch-marker-lint.py"),
                            GOAL_REAL], capture_output=True, text=True)
        ok = p.returncode == 0 and not (p.stdout + p.stderr).strip()
        return "expanded timestamp goal", ok, ["(silent, exit 0)"]


# --- 2. replay the real logged delegate_task refusal -------------------------
# The exact refusal line from errors.log/agent.log, fed back through the
# delegate-failure watchdog: it must surface the refusal and name the marker.

@case("delegate-failure watchdog replays the real logged refusal")
def _refusal_replay():
    cdf = load("check_delegate_failures", REPO / "scripts" / "check_delegate_failures.py")
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "errors.log"
        log.write_text(cdf.REAL + "\n")
        rc, lines = cdf.run_check([str(log)], str(Path(tmp) / "state.json"),
                                  datetime.datetime(2026, 11, 21, 17, 0, 0), mode="peek")
    joined = "\n".join(lines)
    ok = rc == 1 and "delegate_task refusal" in joined and "<UTC-ts>" in joined
    return "real refusal line from 2026-11-21 errors.log", ok, lines


@case("delegate-failure detector self-test (10 assertions, includes the real line)")
def _refusal_selftest():
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "check_delegate_failures.py"),
                        "--self-test"], capture_output=True, text=True)
    out = (p.stderr + p.stdout).strip()
    ok = p.returncode == 0 and "passed" in out
    return "check_delegate_failures --self-test", ok, out.splitlines()


# --- 3. process green, data stale (watchdog jsonl freshness) -----------------
# Real pattern: a writer keeps appending heartbeat rows so the file's mtime
# looks fresh while no real row has landed for hours. file_fresh passes; the
# jsonl check must fail on the age of the newest row.

@case("jsonl writer gone stale under a fresh mtime")
def _jsonl_stale():
    wd = load("watchdog", REPO / "scripts" / "watchdog.py")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.jsonl")
        stale_ms = int((time.time() - 3 * 3600) * 1000)
        with open(path, "w") as f:  # heartbeats keep landing right now ...
            f.write(json.dumps({"kind": "heartbeat", "ok": True}) + "\n")
            f.write(json.dumps({"kind": "heartbeat", "ok": True}) + "\n")
            # ... but the last real data row is three hours old
            f.write(json.dumps({"kind": "event", "ts": stale_ms}) + "\n")
        ok, detail = wd.chk_jsonl_fresh({"path": path, "max_age_hours": 1})
        good = (ok is False) and ("old" in detail)
        return "stale data under fresh mtime", good, [f"chk_jsonl_fresh -> ok={ok}: {detail}"]


@case("healthy jsonl passes (control)")
def _jsonl_fresh():
    wd = load("watchdog", REPO / "scripts" / "watchdog.py")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.jsonl")
        fresh_ms = int((time.time() - 60) * 1000)
        with open(path, "w") as f:
            f.write(json.dumps({"kind": "event", "ts": fresh_ms}) + "\n")
        ok, detail = wd.chk_jsonl_fresh({"path": path, "max_age_hours": 1})
        return "row written a minute ago", bool(ok), [f"chk_jsonl_fresh -> ok={ok}: {detail}"]


@case("heartbeat file never written")
def _heartbeat_missing():
    wd = load("watchdog", REPO / "scripts" / "watchdog.py")
    with tempfile.TemporaryDirectory() as tmp:
        ok, detail = wd.chk_heartbeat({"path": os.path.join(tmp, "ticker_last_success"),
                                       "max_age_hours": 1})
        good = (ok is False) and ("missing" in detail)
        # Scrub the throwaway path so the printed line is reproducible.
        return "absent dead-man file", good, [detail.replace(tmp, "<tmp>")]


# --- 4. skill body names a script that exists but cannot run -----------------
# Real pattern: a helper was deployed without its execute bit. The file
# exists, so a plain existence check passes; the refs gate must still flag it.

@case("skill references a non-executable script")
def _refs_notexec():
    refs = REPO / "bin" / "hermes-skill-refs.py"
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        os.makedirs(home / "bin")
        os.makedirs(home / ".hermes" / "skills" / "backup")
        (home / ".hermes" / "skills" / "backup" / "SKILL.md").write_text(
            "Nightly backup: run `~/bin/backup.sh --full` and check the log.\n")
        (home / "bin" / "backup.sh").write_text("#!/bin/sh\necho backup\n")
        p = subprocess.run([sys.executable, str(refs), "--home", str(home)],
                           capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 1 and "NOT_EXECUTABLE" in out
        # Scrub the throwaway path so the printed lines are reproducible.
        scrubbed = [line.replace(str(home) + os.sep, "<tmp>/") for line in out.splitlines()]
        return "deployed without +x", ok, scrubbed


@case("executable script passes (control)")
def _refs_exec():
    refs = REPO / "bin" / "hermes-skill-refs.py"
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        os.makedirs(home / "bin")
        os.makedirs(home / ".hermes" / "skills" / "backup")
        (home / ".hermes" / "skills" / "backup" / "SKILL.md").write_text(
            "Nightly backup: run `~/bin/backup.sh --full` and check the log.\n")
        (home / "bin" / "backup.sh").write_text("#!/bin/sh\necho backup\n")
        os.chmod(home / "bin" / "backup.sh", 0o755)
        p = subprocess.run([sys.executable, str(refs), "--home", str(home)],
                           capture_output=True, text=True)
        ok = p.returncode == 0 and not (p.stdout + p.stderr).strip()
        return "with +x set", ok, ["(silent, exit 0)"]


# --- 5. a reply that reads as machine-written --------------------------------
# Real pattern: the reply came back fluent and useless - banned phrasing, a
# run of em dashes, a wall of prose. The lint must flag it before it is sent.

BAD_REPLY = (
    "Let me delve into the numbers. It's worth noting that we should leverage "
    "the robust landscape of options when it comes to the migration — and — "
    "moreover — the timeline should be revisited.\n"
    "  - first we inventory the data\n"
    "  - then we map the schemas\n"
    "  - then we cut over\n"
    "  - then we validate\n"
    "  - then we monitor\n"
)
CLEAN_REPLY = ("The migration moves 14 tables. Three of them have no unique key, "
               "so the cutover script will reject rows 2, 407 through 2, 411.\n")


@case("machine-flavored reply is flagged before sending")
def _reply_bad():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reply.md"
        path.write_text(BAD_REPLY)
        p = subprocess.run([sys.executable, str(REPO / "bin" / "hermes-lint-reply.py"),
                            str(path)], capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 1 and "delve" in out and "banned phrase" in out
        return "AI-flavored draft reply", ok, out.splitlines()


@case("plain reply passes (control)")
def _reply_clean():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reply.md"
        path.write_text(CLEAN_REPLY)
        p = subprocess.run([sys.executable, str(REPO / "bin" / "hermes-lint-reply.py"),
                            str(path)], capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 0 and "clean" in out
        return "factual short reply", ok, out.splitlines()


def main() -> int:
    for fn in (_marker_bad, _marker_good, _refusal_replay, _refusal_selftest,
               _jsonl_stale, _jsonl_fresh, _heartbeat_missing,
               _refs_notexec, _refs_exec, _reply_bad, _reply_clean):
        fn()
    if failures:
        print(f"\nFAILED: {len(failures)} case(s) not caught: {', '.join(failures)}")
        return 1
    print("\nall real-world cases caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
