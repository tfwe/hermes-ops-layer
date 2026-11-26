#!/usr/bin/env python3
"""Surface delegate_task refusals and failures that Hermes logged.

Zero tokens, stdlib only. Watchdog contract: silence means healthy.
  exit 0 = no fresh incidents (or self-test passed; self-test prints nothing on pass)
  exit 1 = fresh incidents (printed to stdout) or a self-test assertion failed
  exit 1 also when the logs or state cannot be read: an unverified detector never reads green

An incident is one 'Tool delegate_task returned error' event. The same event is written to
agent.log and errors.log, so incidents are keyed on (timestamp, session id, message) and
counted once however many logs carry them.

State: state/delegate-failures-seen.json maps incident key -> epoch first seen. Each incident
alerts once and is then remembered. Only events within --window-hours are considered, so the
store prunes itself.

Usage:
  check_delegate_failures.py                 watchdog mode: alert on fresh incidents
  check_delegate_failures.py --peek          list fresh incidents, write nothing
  check_delegate_failures.py --baseline      mark every in-window incident as seen, no output
  check_delegate_failures.py --self-test     assert both directions on fixtures, touch no real state
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

HOME = os.path.expanduser("~")
HERMES = os.environ.get("HERMES_HOME") or os.path.join(HOME, ".hermes")
LOGS = [os.path.join(HERMES, "logs", n)
        for n in ("agent.log", "agent.log.1", "errors.log", "errors.log.1")]
STATE = os.path.join(HERMES, "state", "delegate-failures-seen.json")
WINDOW_H = 24.0
EXCERPT = 140
MAX_DETAIL_LINES = 8

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(?P<ms>\d{3}) (?P<lvl>\w+) "
    r"\[(?P<sid>[^\]]*)\] [\w.]+: Tool delegate_task returned error \([\d.]+s\): (?P<payload>.*)$"
)

# A real refusal, verbatim from errors.log and agent.log. Used by the self-test.
REAL = r"""2026-11-21 16:51:54,857 WARNING [20261121_141917_415603] agent.tool_executor: Tool delegate_task returned error (0.01s): {"error": "Task 0 goal contains an unexpanded template marker ('<UTC-ts>'). Substitute the real value before calling delegate_task — subagents cannot resolve placeholders."}"""


class Broken(Exception):
    pass


def excerpt(text, n=EXCERPT):
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 3] + "..."


def parse_line(line):
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    payload = m["payload"]
    msg = payload
    try:
        decoded = json.loads(payload)
        if isinstance(decoded, dict) and decoded.get("error"):
            msg = decoded["error"]
    except ValueError:
        pass  # non-JSON payload: keep the raw text, still an incident
    key = hashlib.sha1(
        ("%s,%s|%s|%s" % (m["ts"], m["ms"], m["sid"], msg)).encode("utf-8")
    ).hexdigest()[:16]
    return {"key": key, "dt": dt, "ts": m["ts"], "sid": m["sid"], "msg": msg}


def collect(paths, now, window_h):
    cutoff = now - datetime.timedelta(hours=window_h)
    ceiling = now + datetime.timedelta(minutes=5)
    found = {}
    readable = 0
    for p in paths:
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue  # rotated logs are optional
        except OSError as e:
            raise Broken("cannot read %s: %s" % (p, e))
        with fh:
            for line in fh:
                if "delegate_task returned error" not in line:
                    continue
                inc = parse_line(line)
                if inc is None or inc["dt"] < cutoff or inc["dt"] > ceiling:
                    continue
                found.setdefault(inc["key"], inc)
        readable += 1
    if readable == 0:
        raise Broken("no Hermes log readable (%s)"
                     % ", ".join(os.path.basename(p) for p in paths))
    return sorted(found.values(), key=lambda i: i["dt"])


def load_seen(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None  # nothing seen yet
    except (OSError, ValueError) as e:
        raise Broken("state %s unreadable: %s" % (path, e))
    seen = data.get("seen") if isinstance(data, dict) else None
    if not isinstance(seen, dict):
        raise Broken("state %s has no 'seen' map" % path)
    return seen


def write_state(path, seen, now_ts):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen": seen, "updated": now_ts}, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def format_lines(fresh):
    if not fresh:
        return []
    first = fresh[0]
    lines = ["%d new delegate_task refusal(s) or failure(s) in Hermes logs; latest %s: %s"
             % (len(fresh), first["ts"], excerpt(first["msg"]))]
    shown = fresh[1:1 + MAX_DETAIL_LINES]
    for inc in shown:
        lines.append("  %s: %s" % (inc["ts"], excerpt(inc["msg"])))
    if len(fresh) - 1 > len(shown):
        lines.append("  … %d more in the last %dh (alert capped at %d detail lines)"
                     % (len(fresh) - 1 - len(shown), WINDOW_H, MAX_DETAIL_LINES))
    return lines


def run_check(paths, state_path, now, window_h=WINDOW_H, mode="alert"):
    """Return (exit_code, output_lines). Writes state only in alert and baseline modes."""
    now_ts = now.timestamp()
    try:
        incidents = collect(paths, now, window_h)
        prior = load_seen(state_path) or {}
    except Broken as e:
        return 1, ["delegate_task failure detector cannot run: %s; refusals are NOT being watched"
                   % e]
    fresh = [i for i in incidents if i["key"] not in prior]
    if mode == "peek":
        return (1 if fresh else 0), format_lines(fresh)
    seen = {k: v for k, v in prior.items() if v >= now_ts - 2 * window_h * 3600}
    for inc in (incidents if mode == "baseline" else fresh):
        seen.setdefault(inc["key"], now_ts)
    try:
        write_state(state_path, seen, now_ts)
    except OSError as e:
        return 1, ["delegate_task failure detector cannot write state %s: %s" % (state_path, e)]
    if mode == "baseline":
        return 0, []
    return (1 if fresh else 0), format_lines(fresh)


def self_test():
    scratch = os.environ.get("TMPDIR") or os.path.join(HERMES, "cache", "scratch")
    os.makedirs(scratch, exist_ok=True)
    base = tempfile.mkdtemp(prefix="dfail-selftest-", dir=scratch)
    results = []

    def write(name, text):
        p = os.path.join(base, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def expect(label, cond):
        results.append((label, bool(cond)))

    now = datetime.datetime(2026, 11, 21, 17, 0, 0)
    agent = write("agent.log", "noise\n" + REAL + "\n")
    errs = write("errors.log", REAL + "\n")
    clean = write("clean.log",
                  "2026-11-21 16:00:00,000 INFO [x] agent.tool_executor: "
                  "Tool terminal returned ok (0.01s): {}\n")
    state = os.path.join(base, "state.json")

    # 1. A real refusal duplicated across both logs: exit 1, names the marker, counted once.
    code, lines = run_check([agent, errs], state, now)
    expect("real refusal exits 1", code == 1)
    expect("alert names the template marker", any("<UTC-ts>" in l for l in lines))
    expect("dual-log event counted once", len(lines) == 1 and lines[0].startswith("1 new"))

    # 2. Same event on the next run: silent (each incident alerts once).
    code, lines = run_check([agent, errs], state, now)
    expect("already-alerted event is silent", code == 0 and lines == [])

    # 3. Clean log: exit 0 and no output.
    code, lines = run_check([clean], os.path.join(base, "clean-state.json"), now)
    expect("clean fixture exits 0 with no output", code == 0 and lines == [])

    # 4. Refusal outside the window: ignored.
    code, lines = run_check([agent], os.path.join(base, "old-state.json"),
                            datetime.datetime(2026, 11, 23, 17, 0, 0))
    expect("out-of-window refusal ignored", code == 0 and lines == [])

    # 5. Baseline marks existing events; a later alert run stays silent for them.
    bstate = os.path.join(base, "baseline.json")
    bcode, blines = run_check([agent], bstate, now, mode="baseline")
    acode, alines = run_check([agent], bstate, now)
    expect("baseline silences existing events", bcode == 0 and blines == []
           and acode == 0 and alines == [])

    # 6. A new event after the baseline is reported (future events are surfaced).
    later = REAL.replace("16:51:54,857", "17:05:00,000").replace("<UTC-ts>", "<chrome ua>")
    newer = write("newer.log", later + "\n")
    code, lines = run_check([newer], bstate, datetime.datetime(2026, 11, 21, 17, 10, 0))
    expect("post-baseline event alerts", code == 1 and bool(lines) and "<chrome ua>" in lines[0])

    # 7. No readable log: fail closed.
    code, lines = run_check([os.path.join(base, "missing.log")],
                            os.path.join(base, "m.json"), now)
    expect("missing logs fail closed", code == 1 and bool(lines) and "cannot run" in lines[0])

    # 8. Corrupt state: fail closed, not a silent full re-alert.
    corrupt = write("corrupt.json", "{not json")
    code, lines = run_check([agent], corrupt, now)
    expect("corrupt state fails closed", code == 1 and bool(lines))

    failed = [label for label, ok in results if not ok]
    shutil.rmtree(base, ignore_errors=True)
    if failed:
        for label in failed:
            print("self-test FAILED: %s" % label)
        return 1
    print("self-test: %d assertions passed" % len(results), file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--log", action="append", help="log file to scan (repeatable)")
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--now", help="fixed clock 'YYYY-MM-DD HH:MM:SS' (testing)")
    ap.add_argument("--window-hours", type=float, default=WINDOW_H)
    ap.add_argument("--peek", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    now = (datetime.datetime.strptime(a.now, "%Y-%m-%d %H:%M:%S") if a.now
           else datetime.datetime.now())
    mode = "peek" if a.peek else "baseline" if a.baseline else "alert"
    code, lines = run_check(a.log or LOGS, a.state, now, a.window_hours, mode)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
