#!/usr/bin/python3
"""hermes-skill-refs: find dead paths and non-executable commands named in skill bodies.

Usage:
  hermes-skill-refs.py [--home DIR] [--allowlist FILE]   check (default)
  hermes-skill-refs.py --report [--home DIR]              per-file counts, exit 0

Scans SKILL.md files under DIR/.hermes/skills (default DIR is the real home) for
single-backtick tokens that start with ~/, /home/, /etc/ or /opt/. The first
whitespace-separated word of the token is the path that is checked.

Skipped as deliberate placeholders (counted in --report): a path containing any of
< > { } * $, or ending in '/'.

Check output, one line per problem, then a count line:
  FULLSKILLPATH:LINE: TOKEN -> RESOLVED                  path does not exist
  FULLSKILLPATH:LINE: TOKEN -> RESOLVED: NOT_EXECUTABLE  file in ~/bin or /usr/local/bin
                                                          lacks the owner-execute bit
Exit: 0 all references resolve (silent), 1 problems found, 2 tool error (stderr).

Allowlist: DIR/.hermes/skills/.refs-allow, one fnmatch glob per line ('#' comments).
A glob matches either the token as written or the resolved path. Missing file = empty.

Read-only: never writes anything.
"""
import argparse
import fnmatch
import os
import re
import stat
import sys

PREFIXES = ("~/", "/home/", "/etc/", "/opt/")
PLACEHOLDER_CHARS = set("<>{}*$")
EXEC_DIRS = ("~/bin", "/usr/local/bin")
TOKEN_RE = re.compile(r"`([^`\n]+)`")


def die(msg):
    print(f"hermes-skill-refs: error: {msg}", file=sys.stderr)
    sys.exit(2)


def expand(path, home):
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    return os.path.normpath(path)


def load_allow(path):
    pats = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        pats.append(ln)
        except OSError as e:
            die(f"cannot read allowlist {path}: {e}")
    return pats


def find_skills(skills_dir):
    if not os.path.isdir(skills_dir):
        die(f"skills directory not found: {skills_dir}")
    out = []
    for root, _dirs, files in os.walk(skills_dir):
        for fn in files:
            if fn == "SKILL.md":
                out.append(os.path.join(root, fn))
    return sorted(out)


def exec_dirs(home):
    return [expand(d, home) for d in EXEC_DIRS]


def scan_file(path, home, allow, stats):
    """Yield (lineno, token, resolved, kind) for each problem; kind is MISSING or NOT_EXECUTABLE."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as e:
        die(f"cannot read {path}: {e}")
    xdirs = exec_dirs(home)
    for n, line in enumerate(lines, 1):
        for m in TOKEN_RE.finditer(line):
            words = m.group(1).split()
            if not words or not words[0].startswith(PREFIXES):
                continue
            token = words[0]
            if any(c in PLACEHOLDER_CHARS for c in token) or token.endswith("/"):
                stats["placeholders"] += 1
                continue
            resolved = expand(token, home)
            if any(fnmatch.fnmatchcase(x, p) for p in allow for x in (token, resolved)):
                continue
            stats["refs"][path] = stats["refs"].get(path, 0) + 1
            if not os.path.exists(resolved):
                stats["missing"][path] = stats["missing"].get(path, 0) + 1
                yield n, token, resolved, "MISSING"
                continue
            parent = os.path.dirname(resolved)
            in_exec = any(parent == d or parent.startswith(d + os.sep) for d in xdirs)
            if in_exec and os.path.isfile(resolved) and not os.stat(resolved).st_mode & stat.S_IXUSR:
                stats["notexec"][path] = stats["notexec"].get(path, 0) + 1
                yield n, token, resolved, "NOT_EXECUTABLE"


def new_stats():
    return {"refs": {}, "missing": {}, "notexec": {}, "placeholders": 0}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Find dead paths in Hermes skill bodies.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="list problems; silent when clean (default)")
    mode.add_argument("--report", action="store_true", help="per-file counts; always exit 0")
    ap.add_argument("--home", default=os.path.expanduser("~"), help="home directory (default: real home)")
    ap.add_argument("--allowlist", default=None, help="glob allowlist (default: SKILLS/.refs-allow)")
    a = ap.parse_args(argv)

    home = os.path.normpath(os.path.abspath(a.home))
    skills_dir = os.path.join(home, ".hermes", "skills")
    allow_path = a.allowlist or os.path.join(skills_dir, ".refs-allow")
    allow = load_allow(allow_path)

    stats = new_stats()
    problems = []
    for path in find_skills(skills_dir):
        for n, token, resolved, kind in scan_file(path, home, allow, stats):
            if kind == "MISSING":
                problems.append(f"{path}:{n}: {token} -> {resolved}")
            else:
                problems.append(f"{path}:{n}: {token} -> {resolved}: NOT_EXECUTABLE")

    if a.report:
        for path in sorted(stats["refs"]):
            print(f"{path}: refs={stats['refs'][path]} missing={stats['missing'].get(path, 0)} "
                  f"not_executable={stats['notexec'].get(path, 0)}")
        print(f"placeholders: {stats['placeholders']}")
        return 0

    for p in problems:
        print(p)
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
