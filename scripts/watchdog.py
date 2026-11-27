#!/usr/bin/env python3
"""Watchdog: proves the persistent things on this host still DO their job.

Design (<USER>'s standing rule):
  - silence means healthy; anything printed is a real failure
  - assertions are data (watchdogs.json), so adding a watched thing is one entry
  - zero tokens: no model, no agent, stdlib only
  - runs from systemd, NOT from Hermes cron, so a dead Hermes ticker cannot
    silence its own watchdog
  - delivers its own Telegram alert, independent of Hermes delivery

Exit codes: 0 = checks ran (text on stdout only if something is broken)
            1 = the watchdog itself is broken (cron/systemd will show it)

Usage: watchdog.py [--json] [--test-alert] [--list]
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
HERMES = os.environ.get("HERMES_HOME") or os.path.join(HOME, ".hermes")
CONFIG = os.environ.get("WATCHDOG_CONFIG") or os.path.join(HERMES, "watchdogs.json")
STATE = os.path.join(HERMES, "state", "watchdog-last-run")
FAIL_LOG = os.path.join(HERMES, "state", "watchdog-failures.jsonl")
ACK_STORE = os.path.join(HERMES, "state", "watchdog-acked.json")
ALERT_CHAT = os.environ.get("WATCHDOG_ALERT_CHAT", "")


# ---------- helpers ----------

def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:  # noqa: BLE001 - any failure here is a check failure
        return 126, "", str(e)


def _age(path):
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return None


def _newest_jsonl_ts(path):
    """Newest `ts` (epoch ms) in a .jsonl capture, reading only the tail."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001 - a torn line is normal on a live writer
            continue
        ts = row.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            # Epoch ms is the contract, but a writer that logs epoch seconds is a
            # one-line mistake that reads as "57 years stale" and pages a false alarm.
            # Distinguish by magnitude rather than trusting the unit.
            return ts / 1000.0 if ts > 1e11 else float(ts)
    return None


def _human_age(seconds):
    if seconds is None:
        return "never"
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds / 60)
    if seconds < 172800:
        return "%.1fh" % (seconds / 3600)
    return "%.1fd" % (seconds / 86400)


def _hours(h):
    return float(h) * 3600.0


# ---------- check implementations: each returns (ok, detail) ----------

def chk_unit(c):
    rc, out, _ = _run("systemctl is-active %s" % c["unit"])
    state = out.splitlines()[0] if out else "unknown"
    return rc == 0 and state == "active", "%s is %s" % (c["unit"], state)


def chk_port(c):
    host, port = c.get("host", "127.0.0.1"), int(c["port"])
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        return True, "port %s:%s accepting" % (host, port)
    except OSError as e:
        return False, "port %s:%s refused (%s)" % (host, port, e.__class__.__name__)
    finally:
        s.close()


def chk_file_fresh(c):
    path = os.path.expanduser(c["path"])
    age = _age(path)
    limit = _hours(c["max_age_hours"])
    if age is None:
        return False, "%s missing" % path
    return age <= limit, "%s last written %s ago (limit %.0fh)" % (
        os.path.basename(path), _human_age(age), limit / 3600)


def chk_jsonl_fresh(c):
    """Freshness of the DATA inside the file, not the file itself.

    This is the check that catches 'process green, nothing arriving': a writer
    that stops writing leaves an old mtime, but a writer that keeps appending
    heartbeats while dropping real messages leaves a fresh mtime and stale data.
    """
    path = os.path.expanduser(c["path"])
    ts = _newest_jsonl_ts(path)
    limit = _hours(c["max_age_hours"])
    if ts is None:
        return False, "no readable rows in %s" % os.path.basename(path)
    age = time.time() - ts
    return age <= limit, "newest row in %s is %s old (limit %.0fh)" % (
        os.path.basename(path), _human_age(age), limit / 3600)


def chk_heartbeat(c):
    """Age of a timestamp file written by some other process (dead-man switch)."""
    path = os.path.expanduser(c["path"])
    age = _age(path)
    limit = _hours(c["max_age_hours"])
    if age is None:
        return False, "heartbeat %s missing — writer never ran" % path
    return age <= limit, "%s beat %s ago (limit %.1fh)" % (
        os.path.basename(path), _human_age(age), limit / 3600)


def chk_cmd(c):
    rc, out, err = _run(c["cmd"], timeout=c.get("timeout", 30))
    detail = (out or err or "").splitlines()
    return rc == 0, (detail[0][:200] if detail else "exit %s" % rc)


def chk_disk(c):
    gb = shutil.disk_usage(c.get("path", "/")).free / 1e9
    return gb >= float(c["min_free_gb"]), "%.1fGB free (floor %.0fGB)" % (gb, float(c["min_free_gb"]))


def chk_mem(c):
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0]) / 1024.0
    mb = info.get("MemAvailable", 0)
    return mb >= float(c["min_available_mb"]), "%.0fMB available (floor %.0fMB)" % (
        mb, float(c["min_available_mb"]))


def chk_cron_jobs(c):
    """Every enabled Hermes cron job must have a plausible next_run_at.

    Catches the scheduler having died or a job being wedged: the job's own
    last_status stays 'ok' forever while it never fires again.
    """
    path = os.path.join(HERMES, "cron", "jobs.json")
    try:
        with open(path) as f:
            jobs = json.load(f).get("jobs", [])
    except Exception as e:  # noqa: BLE001
        return False, "cannot read jobs.json: %s" % e
    now = datetime.now(timezone.utc)
    bad, checked = [], 0
    for j in jobs:
        if not j.get("enabled") or j.get("state") == "paused":
            continue
        nxt = j.get("next_run_at")
        if not nxt:
            bad.append("%s: no next_run_at" % j.get("name", j.get("id")))
            continue
        checked += 1
        try:
            dt = datetime.fromisoformat(nxt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        overdue_h = (now - dt).total_seconds() / 3600
        # a missed slot inside the catch-up window is normal; >6h late is not
        if overdue_h > 6:
            bad.append("%s overdue %.1fh" % (j.get("name", j.get("id")), overdue_h))
    return not bad, ("; ".join(bad) if bad else "%d enabled job(s) on schedule" % checked)


CHECKS = {
    "unit": chk_unit,
    "port": chk_port,
    "file_fresh": chk_file_fresh,
    "jsonl_fresh": chk_jsonl_fresh,
    "heartbeat": chk_heartbeat,
    "cmd": chk_cmd,
    "disk": chk_disk,
    "mem": chk_mem,
    "cron_jobs": chk_cron_jobs,
}


# ---------- delivery ----------

def telegram(text):
    token = None
    env_path = os.path.join(HERMES, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        pass
    if not token:
        return "no TELEGRAM_BOT_TOKEN in .env"
    data = json.dumps({"chat_id": ALERT_CHAT, "text": text,
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return "sent" if r.status == 200 else "HTTP %s" % r.status
    except Exception as e:  # noqa: BLE001
        return "send failed: %s" % e


# ---------- dry run ----------

def dry_run():
    """Validate the config file without running checks, writing state, or alerting.

    Exit 0 when the config parses and every check has a known type and its required
    keys. Exit 1 otherwise. Needs no systemd, no Raspberry Pi, and no Hermes install.
    """
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        print("dry-run: cannot read %s: %s" % (CONFIG, e))
        return 1
    checks = cfg.get("checks")
    if not isinstance(checks, list) or not checks:
        print("dry-run: %s has no 'checks' list" % CONFIG)
        return 1
    required = {"unit": ["unit"], "port": ["port"], "file_fresh": ["path", "max_age_hours"],
                "jsonl_fresh": ["path", "max_age_hours"], "heartbeat": ["path", "max_age_hours"],
                "cmd": ["cmd"], "disk": ["min_free_gb"], "mem": ["min_available_mb"],
                "cron_jobs": []}
    problems = []
    for c in checks:
        name = c.get("name", "<unnamed>")
        kind = c.get("type")
        if kind not in CHECKS:
            problems.append("%s: unknown type %r" % (name, kind))
            continue
        for key in required.get(kind, []):
            if key not in c:
                problems.append("%s: missing %r for type %s" % (name, key, kind))
    for p in problems:
        print("dry-run: " + p)
    print("dry-run: %d check(s) validated, %d problem(s)" % (len(checks), len(problems)))
    return 1 if problems else 0


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--ack", dest="ack", action="append", default=None)
    ap.add_argument("--ack-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the config only: no checks run, no state written, no alert sent")
    a = ap.parse_args()

    if a.dry_run:
        return dry_run()

    if a.test_alert:
        print(telegram("watchdog test: alert path works. %s"
                       % datetime.now().strftime("%Y-%m-%d %H:%M")))
        return 0

    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        msg = ("⚠ Pi watchdog cannot start: unreadable %s (%s) — nothing is being verified"
               % (CONFIG, e))
        print(msg)
        if not a.no_alert:
            telegram(msg)
        return 1

    checks = cfg.get("checks", [])
    if a.list:
        for c in checks:
            print("%-22s %-12s %s" % (c["name"], c["type"], c.get("why", "")))
        return 0

    failures, skipped = [], []
    for c in checks:
        if c.get("enabled") is False:
            skipped.append(c["name"])
            continue
        fn = CHECKS.get(c["type"])
        if fn is None:
            failures.append((c["name"], "unknown check type %r" % c["type"], c.get("why", "")))
            continue
        try:
            ok, detail = fn(c)
        except Exception as e:  # noqa: BLE001 - a broken check is a failure, not a crash
            ok, detail = False, "check raised %s: %s" % (e.__class__.__name__, e)
        if not ok:
            failures.append((c["name"], detail, c.get("why", "")))

    # self-freshness: if the previous run is ancient, the timer is not firing
    prev = _age(STATE)
    if prev is not None and prev > _hours(cfg.get("cadence_hours", 6) * 3):
        failures.append(("watchdog itself",
                         "previous run was %s ago — timer not firing?" % _human_age(prev), ""))

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    now_ts = round(time.time(), 3)
    with open(STATE, "w") as f:
        f.write("%.3f\n" % now_ts)

    checked_count = len(checks) - len(skipped)
    failure_rows = [{"name": n, "detail": d, "why": w} for n, d, w in failures]
    try:
        os.makedirs(os.path.dirname(FAIL_LOG), exist_ok=True)
        with open(FAIL_LOG, "a") as f:
            f.write(json.dumps({"ts": now_ts, "checked": checked_count,
                                "failures": failure_rows}) + "\n")
        with open(FAIL_LOG, "r") as f:
            log_lines = f.readlines()
        if len(log_lines) > 400:
            tmp_log = FAIL_LOG + ".tmp"
            with open(tmp_log, "w") as f:
                f.writelines(log_lines[-400:])
            os.replace(tmp_log, FAIL_LOG)
    except Exception as e:  # noqa: BLE001
        print("watchdog: failed to write %s: %s" % (FAIL_LOG, e), file=sys.stderr)

    to_ack = []
    if a.ack_all:
        to_ack.extend(n for n, _, _ in failures)
    if a.ack:
        to_ack.extend(a.ack)
    to_ack = list(dict.fromkeys(to_ack))
    if to_ack:
        acked_data = {}
        try:
            with open(ACK_STORE) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                acked_data = loaded
        except Exception:  # noqa: BLE001
            acked_data = {}
        ack_ts = time.time()
        for name in to_ack:
            acked_data[name] = ack_ts
        os.makedirs(os.path.dirname(ACK_STORE), exist_ok=True)
        tmp_ack = ACK_STORE + ".tmp"
        with open(tmp_ack, "w") as f:
            json.dump(acked_data, f, indent=2)
            f.write("\n")
        os.replace(tmp_ack, ACK_STORE)
        if not a.json:
            for name in to_ack:
                print("acked: %s (digest will stop repeating it until it fails again)" % name)

    if a.json:
        payload = {"failures": failure_rows, "checked": checked_count}
        if to_ack:
            payload["acked"] = to_ack
        print(json.dumps(payload, indent=2))
        return 0

    if not failures:
        return 0  # silence = healthy, and cron/systemd log nothing

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["⚠ Pi watchdog: %d problem(s), %s" % (len(failures), now), ""]
    for name, detail, why in failures:
        lines.append("• %s — %s" % (name, detail))
        if why:
            lines.append("   (%s)" % why)
    text = "\n".join(lines)
    print(text)
    if not a.no_alert:
        print("\n[telegram] %s" % telegram(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
