#!/usr/bin/env python3
"""Lint an outgoing reply against the scanning-deliverable format spec.

Deterministic, zero tokens, stdlib only. This DETECTS; it never rewrites.
Nothing it does can change how a sentence sounds, because it never touches one.

Spec: skill `grounded-citations` -> `## Presenting Findings to a Scanning Reader`.

Usage:
    hermes-lint-reply.py REPLY.md      print findings; exit 1 if any
    hermes-lint-reply.py - < REPLY.md  read stdin
    hermes-lint-reply.py -q REPLY.md   silent when clean
    hermes-lint-reply.py --thresholds  print the tunable numbers and exit

Exit codes: 0 clean, 1 findings, 2 usage error.

The thresholds are conventions, not measurements. Tune them against what the
reader actually reads; they live at the top of this file on purpose.
"""
from __future__ import annotations

import argparse
import re
import sys

# --- tunable conventions -----------------------------------------------------
WARN_WORDS = 600            # a substantive reply past this is worth restructuring
FLAG_WORDS = 1200           # past this it is a wall, not a reply
LINES_ONE_SCREEN = 40       # "one screen" for the inline-[n] rule
MAX_TABLES = 1              # the spec allows at most one
MAX_EMDASH = 2              # one is fine, a run is not
MAX_PARA_LINES = 4          # a paragraph longer than this is a list or a cut
MAX_LINES_BEFORE_HEADING = 12   # top layer must stand alone
# -----------------------------------------------------------------------------

EMDASH = "\u2014"

BANNED = [
    "delve", "it's worth noting", "worth noting", "when it comes to",
    "in today's", "landscape", "robust", "leverage", "navigate",
    "that said", "at the end of the day", "synergy", "paradigm", "holistic",
    "utilize", "spearhead", "actionable", "great question", "i'd be happy to",
    "as an ai", "it is important to note",
]

RESTATE = re.compile(
    r"^(you asked|your question|to answer your question|in response to your|"
    r"that's a great|good question|let me explain)", re.I)

CITE = re.compile(r"\[\d+\]")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s")
FENCE = re.compile(r"^\s*```")


def classify(text: str):
    """Return list of (lineno, raw, kind) where kind is prose|code|table|heading|sources|blank."""
    rows = []
    in_fence = False
    in_sources = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if FENCE.match(line):
            rows.append((i, line, "code"))
            in_fence = not in_fence
            continue
        if in_fence:
            rows.append((i, line, "code"))
            continue
        if re.match(r"^#{1,6}\s*Sources\b", line, re.I) or re.match(r"^Sources:\s*$", line, re.I):
            in_sources = True
        if in_sources:
            rows.append((i, line, "sources"))
            continue
        if HEADING.match(line):
            rows.append((i, line, "heading"))
        elif line.startswith("|"):
            rows.append((i, line, "table"))
        elif not line.strip():
            rows.append((i, line, "blank"))
        else:
            rows.append((i, line, "prose"))
    return rows


def paragraphs(rows):
    """Yield (start_line, [lines]) for runs of prose separated by anything else."""
    block, start = [], None
    for i, raw, kind in rows:
        if kind == "prose":
            if start is None:
                start = i
            block.append(raw)
        else:
            if block:
                yield start, block
            block, start = [], None
    if block:
        yield start, block


def lint(text: str):
    rows = classify(text)
    prose = [(i, r) for i, r, k in rows if k == "prose"]
    body = [(i, r) for i, r, k in rows if k in ("prose", "table", "heading")]
    words = len(re.findall(r"\S+", " ".join(r for _, r in prose)))
    has_table = any(k == "table" for _, _, k in rows)
    n_tables = sum(1 for idx, (_, _, k) in enumerate(rows)
                   if k == "table" and (idx == 0 or rows[idx - 1][2] != "table"))
    findings = []  # (severity, lineno, message)

    # length
    if words >= FLAG_WORDS:
        findings.append(("FLAG", 1, f"{words:,} words (convention: a scanning reply stays under ~{FLAG_WORDS:,})"))
    elif words >= WARN_WORDS:
        findings.append(("WARN", 1, f"{words:,} words (convention: warn past ~{WARN_WORDS:,})"))

    # structure
    if n_tables > MAX_TABLES:
        first = next(i for i, _, k in rows if k == "table")
        findings.append(("WARN", first, f"{n_tables} table block(s) (spec allows at most {MAX_TABLES})"))

    first_h2 = next((i for i, r, k in rows if k == "heading" and len(HEADING.match(r).group(1)) >= 2), None)
    if first_h2 is not None:
        before = sum(1 for i, r, k in rows if k == "prose" and i < first_h2)
        if before > MAX_LINES_BEFORE_HEADING:
            findings.append(("WARN", first_h2, f"{before} prose lines before the first sub-heading (convention: <= {MAX_LINES_BEFORE_HEADING})"))

    # paragraphs
    for start, block in paragraphs(rows):
        if len(block) > MAX_PARA_LINES:
            findings.append(("WARN", start, f"paragraph runs {len(block)} lines (convention: <= {MAX_PARA_LINES})"))

    # citations vs one screen
    n_lines = len(text.splitlines())
    if n_lines > LINES_ONE_SCREEN:
        for i, r in prose:
            if CITE.search(r):
                findings.append(("FLAG", i, f"inline [n] in a {n_lines}-line reply (spec: plain attribution past {LINES_ONE_SCREEN} lines)"))
                break

    # em dashes: prose, headings and table cells only. A rendered Sources block is
    # full of " - " separators, and code fences are not prose, so counting the whole
    # file reports violations the writer never made.
    total_em = sum(r.count(EMDASH) for _, r, k in rows if k in ("prose", "heading", "table"))
    if total_em > MAX_EMDASH:
        findings.append(("WARN", 1, f"{total_em} em dashes in prose (convention: <= {MAX_EMDASH})"))
    for start, block in paragraphs(rows):
        if sum(b.count(EMDASH) for b in block) >= 3:
            findings.append(("WARN", start, "3+ em dashes in one paragraph"))

    # banned phrases
    low = [(i, r.lower()) for i, r in prose]
    for phrase in BANNED:
        for i, r in low:
            if phrase in r:
                findings.append(("WARN", i, f"banned phrase: {phrase!r}"))
                break

    # dangling references to structures that are not present
    joined = " ".join(r.lower() for _, r in prose)
    for ref, present in (("the table", has_table), ("the chart", has_table),
                         ("the diagram", has_table)):
        if ref in joined and not present:
            findings.append(("FLAG", 1, f"refers to {ref!r} but the reply contains no table/chart"))

    # nested bullets
    prev_indent = -1
    for i, raw, kind in rows:
        m = BULLET.match(raw)
        if m:
            indent = len(m.group(1))
            if indent >= 6:
                findings.append(("WARN", i, f"bullet nested at indent {indent} (spec: no deeper than two levels)"))
                break
            prev_indent = indent

    # shouting headings / restating the question
    for i, raw, kind in rows:
        if kind == "heading":
            t = HEADING.match(raw).group(2).strip()
            if len(t) > 3 and t.isupper():
                findings.append(("WARN", i, f"all-caps heading: {t[:40]}"))
    for i, r in prose:
        if RESTATE.match(r.strip()):
            findings.append(("WARN", i, f"opens by restating the question: {r.strip()[:50]!r}"))
            break

    findings.sort(key=lambda f: (f[1], f[0]))
    return findings, words


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("path", nargs="?", help="markdown file, or - for stdin")
    ap.add_argument("-q", "--quiet", action="store_true", help="print nothing when clean")
    ap.add_argument("--thresholds", action="store_true", help="print the tunable numbers and exit")
    a = ap.parse_args()

    if a.thresholds:
        print(f"WARN_WORDS={WARN_WORDS}\nFLAG_WORDS={FLAG_WORDS}\n"
              f"LINES_ONE_SCREEN={LINES_ONE_SCREEN}\nMAX_TABLES={MAX_TABLES}\n"
              f"MAX_EMDASH={MAX_EMDASH}\nMAX_PARA_LINES={MAX_PARA_LINES}\n"
              f"MAX_LINES_BEFORE_HEADING={MAX_LINES_BEFORE_HEADING}")
        return 0

    if not a.path:
        ap.print_usage(sys.stderr)
        return 2
    try:
        text = sys.stdin.read() if a.path == "-" else open(a.path, encoding="utf-8").read()
    except OSError as e:
        print(f"cannot read {a.path}: {e}", file=sys.stderr)
        return 2

    findings, words = lint(text)
    if not findings:
        if not a.quiet:
            print(f"clean ({words:,} words)")
        return 0

    flags = sum(1 for s, _, _ in findings if s == "FLAG")
    warns = len(findings) - flags
    for sev, lineno, msg in findings:
        print(f"{sev}  L{lineno:<5} {msg}")
    print(f"\n{flags} flag(s), {warns} warning(s)  --  {words:,} words")
    return 1


if __name__ == "__main__":
    sys.exit(main())
