#!/usr/bin/env python3
"""Self-test for the writer-route-gate plugin. Prints nothing when healthy.

Proves the gate still loads, still decides, and still fails open — not merely
that it is installed. Fixtures are synthetic so it costs no tokens and no model
call, and it runs offline.

Exit 0 = healthy. Non-zero = the gate is broken or wrongly registered.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_PLUGINS = Path(os.environ.get("HERMES_PLUGINS_DIR", Path.home() / ".hermes" / "plugins"))
HOME = Path.home() / ".hermes"
PLUGIN = _PLUGINS / "writer-route-gate"

CHILD = (
    "The Pi has 3.8GB usable and Hermes alone runs about 1.2GB of it. "
    "Two OOM kills on November 17, seven minutes apart, each killed a process holding 1.9GB. "
    "Ripgrep walks 139,355 files in 559ms. A 100MB fsync write takes 160 to 180ms. "
    "Thermals are fine at 44.3C with no throttling. "
    "Moving the always-on roles would cost you the out-of-band alert path. "
    "Keep the Pi and run heavy interactive sessions elsewhere. "
    "A bigger box only pays if it removes a hard limit, and memory pressure is the only limit here."
)

COMPLIANT = CHILD + "\n\n**TLDR:** keep the Pi.\n"
CHANGELOGGED = "**Changes:** restored the OOM sentence verbatim.\n\n" + "New prose entirely about a different subject. " * 40


def load():
    spec = importlib.util.spec_from_file_location("wrg_gate", PLUGIN / "gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    problems = []
    if not (PLUGIN / "plugin.yaml").exists():
        problems.append("plugin.yaml missing")
    if not (PLUGIN / "__init__.py").exists():
        problems.append("__init__.py missing")
    try:
        gate = load()
    except Exception as exc:  # noqa: BLE001
        print(f"writer-gate selftest: import failed: {exc}")
        return 2

    # 1. the relay measure still separates a rewrite from a carry-through
    frac = gate.carried_fraction(CHILD, COMPLIANT)
    if frac < 0.99:
        problems.append(f"compliant fixture scored {frac:.2f}, expected 1.0")

    # 2. a reply that rewrote the child is caught
    if gate.carried_fraction(CHILD, CHANGELOGGED) >= gate.OVERLAP_FLOOR:
        problems.append("rewritten fixture scored above the overlap floor")

    # 3. short replies are never touched
    if gate.decide("Two lines only.", "no-such-session") is not None:
        problems.append("short reply was gated")

    # 4. an explicit change log waives the relay check (fail-open on judgement)
    if not gate._CHANGELOG.search(CHANGELOGGED):
        problems.append("change-log detector no longer matches")

    # 5. the hook returns None (not a crash) on a short reply, logging into a throwaway home
    import importlib
    import os
    sys.path.insert(0, str(_PLUGINS))
    saved_home = os.environ.get("HERMES_HOME")
    import tempfile as _tf

    with _tf.TemporaryDirectory() as probe_home:
        os.environ["HERMES_HOME"] = probe_home
        try:
            pkg = importlib.import_module("writer-route-gate")
            if pkg._hook(response_text="short reply", session_id="selftest") is not None:
                problems.append("hook gated a short reply")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"hook raised: {exc}")
        finally:
            if saved_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = saved_home

    # 6. regression: the async-completion shape that produced a live false positive.
    #    A background dispatch lands its result as a role='user' row with
    #    display_kind='async_delegation_complete'; the turn boundary must skip it
    #    or the dispatch falls outside the window and a compliant run gets accused.
    import json
    import sqlite3
    import tempfile

    # 6b. pin the policy: section 6 runs with the real HERMES_HOME restored, where the
    #     author's live writer-gate.json may have relay_notice off; the relay case this
    #     check exercises needs it on. Pin a known config into a throwaway home.
    saved_home_6 = os.environ.get("HERMES_HOME")
    pinned_home = tempfile.mkdtemp(prefix="writer-gate-pin.")
    with open(Path(pinned_home) / "writer-gate.json", "w") as f:
        json.dump({"inline_notice": False, "relay_notice": True, "word_floor": 400,
                   "overlap_floor": 0.55}, f)
    os.environ["HERMES_HOME"] = pinned_home

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
            " content TEXT, tool_calls TEXT, tool_call_id TEXT, display_kind TEXT)"
        )
        call = json.dumps(
            [{"id": "call_1", "call_id": "call_1", "function": {"name": "delegate_task",
              "arguments": json.dumps({"tasks": [{"goal": "draft it", "option": "writer"}]})}}]
        )
        decoy = "[ASYNC DELEGATION BATCH COMPLETE — deleg_BBB] researcher findings. " + (
            "Other unrelated researcher prose about programs and fees. " * 40
        )
        rows = [
            (1, "s1", "user", "how do I decide", None, None, None),
            (2, "s1", "assistant", "", call, None, None),
            (3, "s1", "tool", '{"status": "dispatched", "delegation_id": "deleg_AAA"}', None, "call_1", None),
            (4, "s1", "user", "[ASYNC DELEGATION BATCH COMPLETE — deleg_AAA] " + CHILD, None, None,
             "async_delegation_complete"),
            (5, "s1", "user", decoy, None, None, "async_delegation_complete"),
            (6, "s1", "assistant", "A reply rewritten entirely by the parent. " * 60, None, None, None),
        ]
        conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

        facts = gate.turn_facts("s1", db=db)
        if not facts["dispatched"]:
            problems.append("regression: writer dispatch outside the turn window again")
        if "deleg_BBB" in facts["child_text"]:
            problems.append("regression: gate compared against the researcher block, not the writer's")
        if len(facts["child_text"].split()) < 60:
            problems.append("regression: child text not recovered from the async completion")

        reply = "A reply rewritten entirely by the parent. " * 60
        action, _ = gate.decide_ex(reply, "s1", db=db)
        if action != "relay_notice":
            problems.append(f"regression: rewrite scored '{action}', expected relay_notice")
    if saved_home_6 is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = saved_home_6
    import shutil as _sh
    _sh.rmtree(pinned_home, ignore_errors=True)

    if problems:
        print("writer-gate selftest FAILED: " + "; ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())