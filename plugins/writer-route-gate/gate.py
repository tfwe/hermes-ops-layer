"""Relay check for the writer route.

The parent may brief, fact-check and report. It may not author the sentences a
human reads. Instructions in SOUL.md get the parent to dispatch a `writer`
child, but nothing stops it rewriting that child's text once the facts are on
screen — measured at 45% parent-authored body in the memo run and 100% in the
rent run.

This module is classical code over two strings, with no inference. It runs
inside `transform_llm_output`, sees the finished reply before delivery, and
returns a replacement string or None. It never blocks a turn: a violation
appends the child's own text plus one line saying why, so the user gets both
versions and the transcript records which was which.

Fail-open by design: any exception means "leave the reply alone".
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

WORD_FLOOR = 400          # below this, inline is not in question
OVERLAP_FLOOR = 0.55      # fraction of child sentences the reply must carry
SIMILARITY = 0.85         # a sentence counts as carried at this ratio

# Policy. Overridable at runtime from $HERMES_HOME/writer-gate.json — see _config().
DEFAULTS = {
    "inline_notice": False,   # the footer: log-only, never appended (Carlo, 18 Nov)
    "relay_notice": True,     # the relay case is rare and its text is the useful part
    "word_floor": WORD_FLOOR,
    "overlap_floor": OVERLAP_FLOOR,
}
_CFG_CACHE: tuple = (None, dict(DEFAULTS))

_SYNTHETIC = (
    "hidden",
    "internal_notification",
    "process_complete",
    "async_delegation_complete",
    "steer",
    "failed_turn",
)

_CHANGELOG = re.compile(
    r"^\s*[*_>\-\s]*(changes?|changed|edits?|edited|fix(es|ed)?|diff|modifications?)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def _db_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "state.db"


def _words(text: str) -> int:
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    return len(text.split())


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[#*_>`]", " ", text or "")).strip()


def _sentences(text: str) -> list[str]:
    out = []
    for raw in re.split(r"(?<=[.!?])\s+", _norm(text)):
        if len(raw.split()) > 4:
            out.append(raw.strip())
    return out


def carried_fraction(child: str, parent: str) -> float:
    """Share of the child's sentences that survive in the parent's text."""
    import difflib

    cs, ps = _sentences(child), _sentences(parent)
    if not cs:
        return 1.0
    pset = set(ps)
    kept = sum(
        1
        for s in cs
        if s in pset or difflib.get_close_matches(s, ps, n=1, cutoff=SIMILARITY)
    )
    return kept / len(cs)


def turn_facts(session_id: str, db: Path | None = None) -> dict:
    """What happened in the current turn: did a writer child run, and what did it say?"""
    path = db or _db_path()
    empty = {"dispatched": False, "child_text": ""}
    if not path.exists() or not session_id:
        return empty
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # A turn starts at the last REAL user message. An async delegation completion
        # also lands with role='user', so counting those as boundaries put the very
        # dispatch being checked outside the window (observed false positive, 18 Nov).
        marks = ",".join("?" * len(_SYNTHETIC))
        row = conn.execute(
            f"""SELECT MAX(id) AS m FROM messages
                 WHERE session_id=? AND role='user'
                   AND (display_kind IS NULL OR display_kind NOT IN ({marks}))""",
            (session_id, *_SYNTHETIC),
        ).fetchone()
        start = row["m"] or 0
        rows = list(
            conn.execute(
                """SELECT id, role, content, tool_calls, tool_call_id, display_kind
                     FROM messages
                    WHERE session_id=? AND id>?
                    ORDER BY id""",
                (session_id, start),
            )
        )

        # Which delegate_task calls asked for a writer, and which delegation ids those became.
        writer_calls: set[str] = set()
        for msg in rows:
            if not msg["tool_calls"]:
                continue
            try:
                calls = json.loads(msg["tool_calls"])
            except Exception:
                continue
            for call in calls:
                fn = call.get("function") or {}
                if fn.get("name") != "delegate_task":
                    continue
                args = fn.get("arguments")
                try:
                    parsed = json.loads(args) if isinstance(args, str) else (args or {})
                except Exception:
                    continue
                tasks = parsed.get("tasks") or [parsed]
                if any((t or {}).get("option") == "writer" for t in tasks):
                    writer_calls.add(call.get("call_id") or call.get("id") or "")

        delegations: set[str] = set()
        for msg in rows:
            if msg["role"] != "tool" or msg["tool_call_id"] not in writer_calls:
                continue
            try:
                deleg = json.loads(msg["content"] or "{}").get("delegation_id")
            except Exception:
                continue
            if deleg:
                delegations.add(deleg)

        # The child's text is the async block belonging to that delegation, not just the
        # longest one: a batch's researcher output is longer than the writer's article and
        # comparing against it scores a compliant run at ~5% carried (observed 18 Nov).
        child, fallback = "", ""
        for msg in rows:
            if (msg["display_kind"] or "") != "async_delegation_complete":
                continue
            content = msg["content"] or ""
            if len(content.split()) <= 60:
                continue
            if any(d and d in content for d in delegations):
                if len(content) > len(child):
                    child = content
            elif len(content) > len(fallback):
                fallback = content
        if not child and delegations:
            child = ""          # the writer's block never arrived: no text to compare
        elif not child:
            child = fallback    # no id mapping available: best effort

        dispatched = bool(writer_calls)
    finally:
        conn.close()
    return {"dispatched": dispatched, "child_text": child}


def _closing_block(text: str) -> str:
    """The parent's own critique/TLDR section, which SOUL requires and the child never wrote."""
    for marker in ("**TLDR", "## TLDR", "## Critique"):
        idx = text.find(marker)
        if idx > 0:
            return text[idx:].strip()
    return ""


def _config() -> dict:
    """Policy read from ``$HERMES_HOME/writer-gate.json`` on every call (mtime-cached).

    Policy lives in a file, not in this module: reloading a plugin requires restarting
    the process that serves the user, so anything tunable must be tunable without one.
    """
    global _CFG_CACHE
    path = _db_path().parent / "writer-gate.json"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        _CFG_CACHE = (None, dict(DEFAULTS))
        return dict(DEFAULTS)
    if _CFG_CACHE[0] == stamp:
        return dict(_CFG_CACHE[1])
    try:
        loaded = json.loads(path.read_text())
        merged = {**DEFAULTS, **{k: v for k, v in loaded.items() if k in DEFAULTS}}
    except Exception:
        merged = dict(DEFAULTS)
    _CFG_CACHE = (stamp, merged)
    return dict(merged)


def decide(response_text: str, session_id: str, db: Path | None = None) -> str | None:
    """Return replacement text, or None to leave the reply untouched."""
    return decide_ex(response_text, session_id, db)[1]


def decide_ex(
    response_text: str, session_id: str, db: Path | None = None, *, auditor: bool = False
) -> tuple[str, str | None]:
    """Return ``(action, replacement)``: none / short / inline_notice / relay_notice.

    ``auditor=True`` (the live hook) logs every decision, including none, so an absent
    line means the hook never ran rather than "the run was compliant".
    """
    cfg = _config()
    floor, overlap_floor = int(cfg["word_floor"]), float(cfg["overlap_floor"])
    if not response_text or _words(response_text) <= floor:
        if auditor:
            log_decision(session_id, "short", {"words": _words(response_text or "")})
        return "none", None
    facts = turn_facts(session_id, db)
    child = facts["child_text"]
    overlap = carried_fraction(child, response_text) if child else None

    # A reply that already carries a notice must never collect a second one: the
    # persisted text is fed back in, and appending again stacked two identical blocks.
    already = "**Gate:" in response_text

    verdict = "none"
    replacement = None
    if facts["dispatched"] and child:
        if overlap >= overlap_floor or _CHANGELOG.search(response_text[:800]) or already:
            verdict = "none"
        elif cfg["relay_notice"]:
            closing = _closing_block(response_text)
            notice = (
                f"\n\n---\n\n**Gate: this reply rewrote the writer child's text.** "
                f"It carried {overlap:.0%} of the child's sentences (floor {overlap_floor:.0%}) and "
                f"gave no change log. The child's text follows verbatim; the version above is the "
                f"parent's, and the two differ.\n\n"
            )
            tail = f"\n\n---\n\n{closing}\n" if closing else "\n"
            verdict, replacement = "relay_notice", notice + child + tail
        else:
            verdict = "relay_notice_silent"
    elif not facts["dispatched"]:
        if cfg["inline_notice"] and not already:
            notice = (
                f"\n\n---\n\n**Gate: {_words(response_text)}-word answer written inline, no "
                f"`writer` dispatch in this turn.** Long answers to Carlo are prose a human reads "
                f"and go through the `writer` route.\n"
            )
            verdict, replacement = "inline_notice", response_text + notice
        else:
            verdict = "inline_silent"
    elif facts["dispatched"] and not child:
        verdict = "no_child_text"

    if auditor:
        log_decision(
            session_id,
            verdict,
            {
                "words": _words(response_text),
                "dispatched": facts["dispatched"],
                "overlap": None if overlap is None else round(overlap, 3),
            },
        )
    return verdict, replacement


def log_decision(session_id: str, action: str, detail: dict) -> None:
    """Append one line per decision. Never raises: logging is not worth a broken turn."""
    try:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        path = Path(home) / "logs" / "writer-gate.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"ts": int(time.time() * 1000), "session": session_id, "action": action, **detail}
                )
                + "\n"
            )
    except Exception:
        pass