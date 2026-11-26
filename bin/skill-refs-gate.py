#!/usr/bin/python3
"""skill-refs-gate: alert on NEW dead paths in skill bodies, not on the known backlog.

Why this exists: the watchdog's first version compared the *count* of problems with a
recorded baseline number. A count cannot tell a fix from an addition -- fixing one dead
path while a new skill introduces another keeps the number at 43 and the gate stays
silent, which is exactly the class of regression it was built to catch (measured
2026-11-22: a new skill's wrong-case path took 43 -> 44, and a same-day fix would have
hidden it).

Baseline is a set of identities, not a number: one `SKILL: token` per known problem,
with the line number dropped so editing a file (which moves lines) does not read as a
new problem. Baseline file lives inside the skills tree (~/.hermes/skills/.refs-baseline)
so it is carried by hermes-sync; a missing baseline therefore means something removed
it, and this gate fails loudly rather than silently re-seeding.

Exit: 0 when every current problem is already recorded (silent), 1 when a new one
appeared (prints each new line), 2 on tool error / missing baseline.
"""
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
SCANNER = os.path.join(HOME, "bin", "hermes-skill-refs.py")
SKILLS = os.path.join(os.environ.get("HERMES_HOME", os.path.join(HOME, ".hermes")), "skills")
BASELINE = os.path.join(SKILLS, ".refs-baseline")


def scan():
    if not os.access(SCANNER, os.X_OK):
        print("skill-refs-gate: scanner missing or not executable: %s" % SCANNER)
        sys.exit(2)
    p = subprocess.run([SCANNER], capture_output=True, text=True, timeout=300)
    lines = []
    for line in p.stdout.splitlines():
        if not line.strip() or line.strip().endswith("problem(s)"):
            continue
        # "<skill path>:<line>: <token> -> <resolved>[: NOT_EXECUTABLE]"
        try:
            skill, _ln, rest = line.split(":", 2)
        except ValueError:
            continue
        token, _, verdict = rest.partition(" -> ")
        token = token.strip()
        if not token:
            continue
        lines.append((os.path.dirname(skill), token, line, verdict.strip()))
    return p.returncode, lines


def load_baseline():
    out = set()
    with open(BASELINE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def ident(skill_dir, token):
    rel = os.path.relpath(skill_dir, SKILLS)
    return "%s: %s" % (rel, token)


def main():
    try:
        rc, lines = scan()
        known = load_baseline()
    except FileNotFoundError:
        print("skill-refs-gate: baseline missing at %s - re-seed it deliberately, "
              "do not let the gate re-seed itself" % BASELINE)
        sys.exit(2)
    except Exception as e:  # noqa: BLE001 - a broken gate must not read green
        print("skill-refs-gate: %s" % e)
        sys.exit(2)

    if rc == 2:
        print("skill-refs-gate: scanner error (exit 2)")
        sys.exit(2)

    current = {}
    for skill_dir, token, raw, _verdict in lines:
        current.setdefault(ident(skill_dir, token), raw)

    new = sorted(set(current) - known)
    if new:
        print("%d new dead skill reference(s)" % len(new))
        for key in new:
            print("  " + current[key])
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()