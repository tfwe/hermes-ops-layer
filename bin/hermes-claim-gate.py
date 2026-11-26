#!/usr/bin/python3
"""hermes-claim-gate: refuse duplicate declarations of route and policy facts outside their owner.

Owner table (v1): config.yaml owns route names, model ids and prices. SOUL.md owns the
delegation/escalation ladder, the writer/writer-internal split and the critique format.
Memory files name no route at all.

Usage:
  hermes-claim-gate.py [--home DIR] [--allowlist FILE] [--report | --json]
  hermes-claim-gate.py [--home DIR] [--allowlist FILE] --text [--as LABEL] < entry.txt

--text: read one proposed memory entry (the full text) from stdin and apply only HARD rules
a and b, as if it were a memory file. LABEL (default '<stdin>') names the source for the
output prefix and for allowlist matching, e.g. memories/MEMORY.md.

Exit: 0 clean (silent by default), 1 HARD violations (one 'path:line: violation: ...' per line
on stdout), 2 tool error (stderr). Read-only: it never writes a file, including under --json.

HARD rules:
  a. memories/MEMORY.md or USER.md names a route name or model id from config.yaml.
  b. a memory file has one line with coder/repo-coder AND an escalation word (escalat*,
     failed twice, only after): that restates the escalation policy, which SOUL.md owns.
  c. SOUL.md outside the ROUTES block names a model id that config.yaml does not carry.

SOFT (--report, --json; never fails the run): skill files and references that name a route
or model id. A reference is a route name in backticks (`coder`) or any model id. This is the
cleanup backlog, not a violation.

Allowlist: optional FILE (default <home>/skills/.claims-allow; missing means empty), one
'path:pattern' per line, '#' comments. path is an fnmatch glob relative to the home dir;
pattern is an fnmatch glob matched against the whole offending source line, e.g.
  memories/MEMORY.md:*gemini-app*

Reuses the config parsing and ROUTES block finder from hermes-prompt-sync.py.
"""
import argparse
import fnmatch
import importlib.util
import json
import os
import re
import sys

sys.dont_write_bytecode = True  # read-only tool: do not drop __pycache__ next to the sync module

HERE = os.path.dirname(os.path.abspath(__file__))
ESC_RE = re.compile(r"escalat|failed twice|only after", re.I)
CODER_RE = re.compile(r"\bcoder\b", re.I)  # matches coder and repo-coder


def die(msg):
    print(f"hermes-claim-gate: error: {msg}", file=sys.stderr)
    sys.exit(2)


def load_sync():
    path = os.path.join(HERE, "hermes-prompt-sync.py")
    try:
        spec = importlib.util.spec_from_file_location("hermes_prompt_sync", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (OSError, ImportError, SyntaxError) as e:
        die(f"cannot load parser from {path}: {e}")
    return mod


SYNC = load_sync()


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        die(f"cannot read {path}: {e}")


def alt(words):
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def build_facts(home):
    routes, default = SYNC.load_live(home)
    names = {r["name"] for r in routes}
    ids = {r["model"] for r in routes} | {default[1]}
    suffixes = {i.split("/")[-1] for i in ids}
    return {
        "names": names,
        "ids": ids,
        "suffixes": suffixes,
        "name_re": re.compile(rf"(?<![\w-])({alt(names)})(?![\w-])"),
        # A model id, full or bare, as a token. Lookahead lets a sentence-final '.' through.
        "id_re": re.compile(rf"(?<![\w/.-])({alt(ids | suffixes)})(?![\w/-]|\.\w)"),
        "tick_re": re.compile(rf"`({alt(names)})`"),
    }


def parse_allowlist(path):
    entries = []
    if not os.path.exists(path):
        return entries
    for raw in read_text(path).split("\n"):
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if ":" not in ln:
            die(f"bad allowlist line in {path}: '{ln}' (want path:pattern)")
        p, pat = ln.split(":", 1)
        entries.append((p.strip(), pat.strip()))
    return entries


def allowed(rel, line, allow):
    return any(fnmatch.fnmatchcase(rel, p) and fnmatch.fnmatchcase(line, pat) for p, pat in allow)


def memory_violations(rel, lines, facts, allow):
    out = []
    for n, s in enumerate(lines, 1):
        if allowed(rel, s, allow):
            continue
        for name in dict.fromkeys(m.group(1) for m in facts["name_re"].finditer(s)):
            out.append(f"{rel}:{n}: violation: route name '{name}' in memory; "
                       f"route names belong only in config.yaml")
        for tok in dict.fromkeys(m.group(1) for m in facts["id_re"].finditer(s)):
            out.append(f"{rel}:{n}: violation: model id '{tok}' in memory; "
                       f"model ids belong only in config.yaml")
        if CODER_RE.search(s) and ESC_RE.search(s):
            out.append(f"{rel}:{n}: violation: restates the escalation policy (coder + escalation "
                       f"wording); the ladder belongs only in SOUL.md")
    return out


def model_shaped(tok):
    """A model id has a version digit or a known model family; 'device/ops' and 'firewall/network' do not."""
    tail = tok.split("/")[-1]
    return bool(re.search(r"\d", tail) or re.match(r"(gpt|claude|gemini|deepseek|glm)-", tail))


def soul_violations(path, rel, text, facts, allow):
    span = SYNC.find_block(text, path)
    skip = set()
    if span:
        b, e = span
        start = text[:b].count("\n") + 1
        skip = set(range(start, start + text[b:e].count("\n") + 1))
    out = []
    for n, s in enumerate(text.split("\n"), 1):
        if n in skip or allowed(rel, s, allow):
            continue
        toks = [m.group(1) for m in SYNC.ID_RE.finditer(s)] + \
               [m.group(1) for m in SYNC.BARE_RE.finditer(s)]
        for tok in dict.fromkeys(toks):
            if tok in facts["names"] or not model_shaped(tok):
                continue  # a route name is not a model id; 'firewall/network' is prose
            if tok not in facts["ids"] and tok not in facts["suffixes"]:
                out.append(f"{rel}:{n}: violation: model id '{tok}' is not in config.yaml; "
                           f"SOUL names model ids only inside the ROUTES block")
    return out


def hard_violations(home, facts, allow):
    out = []
    soul = os.path.join(home, "SOUL.md")
    if os.path.exists(soul):
        out += soul_violations(soul, os.path.relpath(soul, home), read_text(soul), facts, allow)
    for name in ("MEMORY.md", "USER.md"):
        p = os.path.join(home, "memories", name)
        if os.path.exists(p):
            out += memory_violations(os.path.relpath(p, home), read_text(p).split("\n"),
                                     facts, allow)
    return out


def soft_counts(home, facts):
    skills = os.path.join(home, "skills")
    res = {"skill_files": 0, "files_naming": 0, "references": 0, "files": []}
    if not os.path.isdir(skills):
        return res
    for root, _dirs, files in os.walk(skills):
        for fn in sorted(files):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(root, fn)
            res["skill_files"] += 1
            refs = 0
            for s in read_text(p).split("\n"):
                refs += len(facts["tick_re"].findall(s)) + len(facts["id_re"].findall(s))
            if refs:
                res["files_naming"] += 1
                res["references"] += refs
                res["files"].append(os.path.relpath(p, home))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refuse route/policy facts outside their owner file.")
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    ap.add_argument("--allowlist", default=None, help="default: <home>/skills/.claims-allow")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--report", action="store_true", help="also print SOFT counts")
    grp.add_argument("--json", action="store_true", help="print JSON; writes nothing")
    grp.add_argument("--text", action="store_true", help="check one proposed memory entry from stdin")
    ap.add_argument("--as", dest="label", default="<stdin>", help="source label for --text (e.g. memories/MEMORY.md)")
    a = ap.parse_args(argv)

    home = os.path.abspath(a.home)
    if not os.path.isdir(home):
        die(f"home is not a directory: {home}")
    facts = build_facts(home)
    allow_path = a.allowlist or os.path.join(home, "skills", ".claims-allow")
    allow = parse_allowlist(allow_path)
    if a.text:
        # Memory-file rules (a) and (b) only; rule (c) is SOUL-only and does not apply to an entry.
        text = sys.stdin.read()
        hard = memory_violations(a.label, text.split("\n"), facts, allow)
        for v in hard:
            print(v)
        return 1 if hard else 0
    hard = hard_violations(home, facts, allow)
    soft = soft_counts(home, facts) if (a.report or a.json) else None

    if a.json:
        print(json.dumps({
            "violations": [v for v in hard],
            "soft": soft,
            "exit": 1 if hard else 0,
        }, indent=2))
    else:
        for v in hard:
            print(v)
        if a.report:
            print(f"soft: skill files naming a route or model id: {soft['files_naming']} of "
                  f"{soft['skill_files']}; references: {soft['references']}")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
