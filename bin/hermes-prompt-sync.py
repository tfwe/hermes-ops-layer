#!/usr/bin/python3
"""hermes-prompt-sync: keep model claims in Hermes prompt files equal to config.yaml.

Usage:
  hermes-prompt-sync.py --check  [--home DIR] [--allowlist FILE] [--price MODEL=IN/OUT ...]
  hermes-prompt-sync.py --render [--home DIR]

Exit: 0 healthy (silent under --check), 1 drift (one line per problem on stdout),
      2 tool error (stderr).

--render rewrites only the text between <!-- ROUTES:BEGIN --> and <!-- ROUTES:END --> in
SOUL.md, appending the marked block if the markers are absent. The block is a pure function
of config.yaml, so a second run leaves the file byte-identical. It never writes config.yaml.

--check also compares the ROUTES block with the live config, then scans SOUL.md,
memories/MEMORY.md and memories/USER.md outside the block for:
  1. a provider/model id that is in no live route and not in the allowlist
  2. a route name followed (same line, within WINDOW chars) by a model that is not its model
  3. a $in/$out price pair that contradicts the priced model named on the same line
  4. a route name that is not in model_map (e.g. a name left over after a rename)
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile

import yaml

BEGIN = "<!-- ROUTES:BEGIN -->"
END = "<!-- ROUTES:END -->"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ALLOW = os.path.join(HERE, "hermes-prompt-sync.allow")
WINDOW = 60  # chars after a route name in which its model must be named

# Per-1M-token prices ($in, $out). Source: route descriptions in config.yaml.
# A list allows tiered prices. Override or add with --price MODEL=IN/OUT.
DEFAULT_PRICES = {
    "anthropic/claude-haiku-5.5": [(0.10, 0.50), (0.50, 2.50)],  # base tier, >100k tier
    "google/gemini-4-hydrogen": [(1.50, 7.50)],
}

ID_RE = re.compile(
    r"(?<![\w./~-])([a-z0-9](?:[a-z0-9._-]*[a-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
BARE_RE = re.compile(
    r"(?<![\w./-])((?:gpt|claude|gemini|deepseek|glm)-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)")
ROUTE_RE = re.compile(r"(?<![\w-])(repo-coder|writer-internal|coder|reviewer|researcher|verifier|writer)(?![\w-])")
ROLE_RE = re.compile(r"(?<![\w-])([a-z][a-z0-9]*-(?:coder|reviewer|researcher|verifier|writer))(?![\w-])")
PRICE_RE = re.compile(r"\$(\d+(?:\.\d+)?)\s*/\s*\$(\d+(?:\.\d+)?)")
BLOCK_ROUTE_RE = re.compile(r"^- `([^`]+)`: (\S+) / `([^`]+)`\.")
BLOCK_DEFAULT_RE = re.compile(r"^Default child \(delegation\.model\): (\S+) / `([^`]+)`\.")


def die(msg):
    print(f"hermes-prompt-sync: error: {msg}", file=sys.stderr)
    sys.exit(2)


def first_sentence(desc):
    m = re.search(r"\.(?=\s|$)", desc)
    return desc[: m.end()] if m else desc


def load_live(home):
    path = os.path.join(home, "config.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        d = cfg["delegation"]
        opts = d["backends"]["model_map"]["options"]
        default = (str(d["provider"]), str(d["model"]))
    except OSError as e:
        die(f"cannot read {path}: {e}")
    except yaml.YAMLError as e:
        die(f"cannot parse {path}: {e}")
    except (KeyError, TypeError) as e:
        die(f"{path} lacks delegation.backends.model_map.options or delegation.model/provider ({e})")
    routes = []
    for o in opts or []:
        try:
            routes.append({
                "name": str(o["name"]),
                "provider": str(o["provider"]),
                "model": str(o["model"]),
                "desc": " ".join(str(o.get("description", "")).split()),
            })
        except (KeyError, TypeError) as e:
            die(f"malformed model_map option in {path}: {o!r} ({e})")
    names = [r["name"] for r in routes]
    if not routes or len(set(names)) != len(names):
        die(f"model_map in {path} is empty or has duplicate names")
    return routes, default


def block_text(routes, default):
    lines = [BEGIN,
             "Live routes, generated from config.yaml by hermes-prompt-sync.py. Do not edit by hand."]
    for r in routes:
        fs = first_sentence(r["desc"])
        lines.append(f"- `{r['name']}`: {r['provider']} / `{r['model']}`. {fs}".rstrip())
    lines.append(f"Default child (delegation.model): {default[0]} / `{default[1]}`.")
    lines.append(END)
    return "\n".join(lines)


def find_block(text, path):
    nb, ne = text.count(BEGIN), text.count(END)
    if nb == 0 and ne == 0:
        return None
    if nb != 1 or ne != 1:
        die(f"{path}: need exactly one {BEGIN} and one {END}")
    b, e = text.index(BEGIN), text.index(END)
    if e < b:
        die(f"{path}: {END} precedes {BEGIN}")
    return b, e + len(END)


def block_diff(path, start, actual, routes, default, names):
    out = []
    seen, dseen = {}, None
    for i, ln in enumerate(actual.split("\n")):
        m = BLOCK_ROUTE_RE.match(ln)
        if m:
            seen[m.group(1)] = (m.group(2), m.group(3), start + i)
        m = BLOCK_DEFAULT_RE.match(ln)
        if m:
            dseen = (m.group(1), m.group(2), start + i)
    for r in routes:
        got = seen.get(r["name"])
        if got is None:
            out.append(f"{path}:{start}: route '{r['name']}': missing from ROUTES block; "
                       f"config says {r['provider']} / {r['model']}")
        elif (got[0], got[1]) != (r["provider"], r["model"]):
            out.append(f"{path}:{got[2]}: route '{r['name']}': ROUTES block says {got[0]} / {got[1]}, "
                       f"config says {r['provider']} / {r['model']}")
    for name, got in seen.items():
        if name not in names:
            out.append(f"{path}:{got[2]}: route '{name}': in ROUTES block but not in model_map")
    if dseen is None:
        out.append(f"{path}:{start}: default child line missing from ROUTES block; "
                   f"config says {default[0]} / {default[1]}")
    elif (dseen[0], dseen[1]) != default:
        out.append(f"{path}:{dseen[2]}: default child: ROUTES block says {dseen[0]} / {dseen[1]}, "
                   f"config says {default[0]} / {default[1]}")
    if not out:
        out.append(f"{path}:{start}: ROUTES block text is stale; run hermes-prompt-sync.py --render")
    return out


def scan_line(path, n, s, by_name, live_ids, allow, prices):
    out = []
    ids = [(m.start(), m.group(1)) for m in ID_RE.finditer(s)]
    for _, mid in ids:
        if mid not in live_ids and mid not in allow:
            out.append(f"{path}:{n}: model id '{mid}' is in no route (model_map)")

    routes_here = [(m.start(), m.group(1)) for m in ROUTE_RE.finditer(s)]
    toks = sorted(ids + [(m.start(), m.group(1)) for m in BARE_RE.finditer(s)])
    for idx, (p, name) in enumerate(routes_here):
        if name not in by_name:
            continue  # reported by the route-name check below
        end = routes_here[idx + 1][0] if idx + 1 < len(routes_here) else len(s)
        end = min(end, p + WINDOW)
        nxt = next(((q, t) for q, t in toks if p < q < end and t not in allow), None)
        if nxt is None:
            continue
        want = by_name[name]["model"]
        tok = nxt[1]
        ok = tok == want if "/" in tok else tok == want.split("/")[-1]
        if not ok:
            out.append(f"{path}:{n}: route '{name}': text says '{tok}', "
                       f"config says '{want}' ({by_name[name]['provider']})")

    if PRICE_RE.search(s):
        cands = [mid for _, mid in ids if mid in prices]
        cands += [by_name[nm]["model"] for _, nm in routes_here
                  if nm in by_name and by_name[nm]["model"] in prices]
        cands = list(dict.fromkeys(cands))
        if cands:
            for m in PRICE_RE.finditer(s):
                pair = (float(m.group(1)), float(m.group(2)))
                if not any(abs(pair[0] - x) < 1e-9 and abs(pair[1] - y) < 1e-9
                           for c in cands for x, y in prices[c]):
                    c = cands[0]
                    known = ", ".join(f"${x:g}/${y:g}" for x, y in prices[c])
                    out.append(f"{path}:{n}: price ${m.group(1)}/${m.group(2)} contradicts "
                               f"{c} ({known} per 1M)")

    for m in ROLE_RE.finditer(s):
        routes_here.append((m.start(), m.group(1)))
    for _, nm in routes_here:
        if nm not in by_name:
            out.append(f"{path}:{n}: route name '{nm}' is not in model_map "
                       f"(live: {', '.join(sorted(by_name))})")
    return out


def check(home, allow, overrides):
    routes, default = load_live(home)
    by_name = {r["name"]: r for r in routes}
    live_ids = {r["model"] for r in routes} | {default[1]}
    prices = dict(DEFAULT_PRICES)
    prices.update(overrides)
    unpriced = []
    for r in routes:
        model = r["model"]
        if PRICE_RE.search(r.get("description") or "") and model not in prices:
            unpriced.append(
                f"config.yaml: {r['name']} quotes a price for {model} that DEFAULT_PRICES does not carry; "
                f"add it with --price {model}=IN/OUT or DEFAULT_PRICES"
            )

    soul = os.path.join(home, "SOUL.md")
    try:
        with open(soul, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        die(f"cannot read {soul}: {e}")

    problems = list(unpriced)
    skip = set()
    span = find_block(text, soul)
    if span is None:
        problems.append(f"{soul}:1: ROUTES block missing; run hermes-prompt-sync.py --render")
    else:
        b, e = span
        start = text[:b].count("\n") + 1
        skip = set(range(start, start + text[b:e].count("\n") + 1))
        if text[b:e] != block_text(routes, default):
            problems += block_diff(soul, start, text[b:e], routes, default, set(by_name))

    targets = [(soul, text.split("\n"), skip)]
    for name in ("MEMORY.md", "USER.md"):
        p = os.path.join(home, "memories", name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    targets.append((p, f.read().split("\n"), set()))
            except OSError as e:
                die(f"cannot read {p}: {e}")

    for path, lines, skp in targets:
        for n, s in enumerate(lines, 1):
            if n in skp:
                continue
            problems += scan_line(path, n, s, by_name, live_ids, allow, prices)

    jobs_path = os.path.join(home, "cron", "jobs.json")
    if os.path.exists(jobs_path):
        try:
            with open(jobs_path, encoding="utf-8") as f:
                jobs = json.load(f)
        except (OSError, ValueError) as e:
            die(f"cannot read {jobs_path}: {e}")
        if isinstance(jobs, dict):
            jobs = jobs.get("jobs") or []
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            label = f"{jobs_path}:{job.get('id', '?')}"
            for n, s in enumerate(str(job.get("prompt") or "").split("\n"), 1):
                problems += scan_line(label, n, s, by_name, live_ids, allow, prices)

    # Skills also tell the model what a route runs. Narrow rule on purpose: only a route
    # name followed by a model id in the same line, so measured history and bare model
    # mentions do not turn --check permanently red.
    skills_dir = os.path.join(home, "skills")
    if os.path.isdir(skills_dir):
        for root, _dirs, files in os.walk(skills_dir):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        lines = f.read().split("\n")
                except OSError:
                    continue
                for n, s in enumerate(lines, 1):
                    for m in ROUTE_RE.finditer(s):
                        # Prose uses these words too ("code writer"). In a skill, a route name is
                        # written as `coder`, so require the backticks before treating it as a claim.
                        if f"`{m.group(1)}`" not in s:
                            continue
                        tail = s[m.end():m.end() + 80]
                        hit = ID_RE.search(tail) or BARE_RE.search(tail)
                        if hit is None:
                            continue
                        name, claim = m.group(1), hit.group(1)
                        want = (by_name.get(name) or {}).get("model")
                        if not want:
                            problems.append(f"{p}:{n}: retired route name '{name}'")
                        elif claim != want and claim != want.split("/")[-1]:
                            problems.append(f"{p}:{n}: route '{name}' says '{claim}', config says '{want}'")
    return problems


def load_allow(path):
    allow = set()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        allow.add(ln.split()[0])
        except OSError as e:
            die(f"cannot read allowlist {path}: {e}")
    return allow


def parse_prices(items):
    out = {}
    for it in items:
        m = re.fullmatch(r"([^=\s]+)=\$?(\d+(?:\.\d+)?)/\$?(\d+(?:\.\d+)?)", it)
        if not m:
            die(f"bad --price '{it}', want MODEL=IN/OUT")
        out[m.group(1)] = [(float(m.group(2)), float(m.group(3)))]
    return out


def render(home):
    routes, default = load_live(home)
    path = os.path.join(home, "SOUL.md")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        die(f"cannot read {path}: {e}")
    expected = block_text(routes, default)
    span = find_block(text, path)
    if span is None:
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + expected + "\n"
    else:
        b, e = span
        new = text[:b] + expected + text[e:]
    if new == text:
        print("render: unchanged")
        return 0
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".soul-render-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    print("render: ROUTES block updated in SOUL.md")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keep Hermes prompt model claims equal to config.yaml.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 and list drift; silent when healthy")
    mode.add_argument("--render", action="store_true", help="rewrite the ROUTES block in SOUL.md")
    ap.add_argument("--home", default=os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    ap.add_argument("--allowlist", default=DEFAULT_ALLOW)
    ap.add_argument("--price", action="append", default=[], metavar="MODEL=IN/OUT")
    ap.add_argument("--brief", action="store_true",
                    help="with --check: one line (count + first problem), for the watchdog cmd check")
    a = ap.parse_args(argv)
    if a.render:
        return render(a.home)
    problems = check(a.home, load_allow(a.allowlist), parse_prices(a.price))
    prefix = a.home.rstrip("/") + "/"
    problems = [p.replace(prefix, "") for p in problems]
    if a.brief and problems:
        msg = f"{len(problems)} drift problem(s); first: {problems[0][:160]}"
        if len(problems) > 1:
            msg += f" (+{len(problems) - 1} more)"
        print(msg)
    else:
        for p in problems:
            print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
