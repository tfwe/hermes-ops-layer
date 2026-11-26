"""Validation for delegate_task dispatch envelopes.

Envelope checks are synchronous, in-memory work. The tool-free gate is the one
exception: it reads ``config.yaml`` once (cached) to learn which routes are
gemini-app, and stats a CONTEXT_PATH/CONTEXT_FOLDER argument to confirm it
exists. No subprocesses, network access, or sleeps. It returns a host
``pre_tool_call`` block directive for malformed calls and None for calls that
should proceed. The hook wrapper in ``__init__.py`` catches every unexpected
exception and fails open.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_ALLOWED_ROUTES = frozenset({"coder", "reviewer", "researcher", "verifier", "writer"})
_ENVELOPE_KEYS = ("TASK", "FACTS", "PATHS", "OUTPUT", "CHECK", "CEILING")
_LEADING_MARKDOWN = r"^[\s>*#\-+_`~]*"
_PLACEHOLDER_WORDS = frozenset({"placeholder", "todo", "tbd", "xxx", "lorem", "ipsum"})
_LOREM_IPSUM = re.compile(r"lorem\s+ipsum", re.IGNORECASE)
_WRITER_FACTS_VIOLATION = (
    'brief.FACTS: the writer route must carry the facts to use; supply real facts or a source pointer, not "none"'
)
_WRITER_FACTS_NEGATIONS = frozenset({"none", "n/a", "self-contained", "no facts", "n/a - the model knows"})

# Brief size: a pasted corpus bills at full fresh-input rate on a Sonnet child, so the
# cheap form is always a file path the child reads for itself. Measured on this host: one
# reviewer dispatch carrying 617k tokens cost more than the rest of the month's dispatches
# put together. Thresholds are characters (~4 chars/token), checked without any I/O.
_BRIEF_SOFT_CHARS = 120_000  # ~30k tokens: allowed only when the brief cites a path
_BRIEF_HARD_CHARS = 240_000  # ~60k tokens: refuse outright, file offload required
_PATH_CITATION = re.compile(r"""(?:^|[\s("'`])(?:~|/)[\w.@%+-]{3,}""")

_ROUTES_CACHE: frozenset[str] | None = None
_MODEL_MAP_KEY = re.compile(r"^(\s*)model_map:\s*$")
_OPTION_NAME = re.compile(r"^-\s*name:\s*[\"']?([A-Za-z0-9._-]+)")
_PROVIDER_LINE = re.compile(r"^provider:\s*[\"']?([A-Za-z0-9._/-]+)")

# Tool-free routes: a child on the gemini-app provider has no filesystem and no shell,
# so a brief that names local files is answered from prose alone. Resolved from config
# (provider == gemini-app); the fallback applies only when config cannot be read.
_TOOL_FREE_PROVIDER = "gemini-app"
_TOOL_FREE_FALLBACK = frozenset({"writer", "gemini-app"})
_TOOL_FREE_CACHE: frozenset[str] | None = None
_CONTEXT_PATH_KEY = "CONTEXT_PATH"
_CONTEXT_FOLDER_KEY = "CONTEXT_FOLDER"
_CONTEXT_NONE_KEY = "CONTEXT_NONE"
_CONTEXT_ROOT = os.environ.get("DISPATCH_CONTEXT_ROOT") or str(Path.home())
_ARTIFACT_PATTERNS = (
    # (a) absolute paths under the context root or ~/.hermes
    re.compile(
        r"(?<![\w/.~-])(?:"
        + re.escape(_CONTEXT_ROOT)
        + r"|~/\.hermes)(?![\w.-])(?:/[^\s\"'`<>()\[\]{},;]*)?"
    ),
    # (b) literal config.yaml / SOUL.md filenames
    re.compile(r"(?<![\w./~-])(?:config\.yaml|SOUL\.md)(?![\w-]|\.\w)", re.IGNORECASE),
    # (c) filename-shaped tokens ending in .py .yaml .json .sh .md
    re.compile(r"(?<![\w.~-])[\w./~-]*\w\.(?:py|yaml|json|sh|md)(?![\w-]|\.\w)"),
)

# In-memory count of refusals per defect set, bounded to _MAX_REPEAT_KEYS entries.
_REPEAT_COUNTS: dict[tuple[str, ...], int] = {}
_MAX_REPEAT_KEYS = 64


def _defect_kinds(violations: Any) -> tuple[str, ...]:
    """Map violation strings to their kinds (text before the first colon) as a sorted unique tuple."""
    kinds: set[str] = set()
    for violation in violations:
        text = str(violation)
        kind = text.split(":", 1)[0] if ":" in text else text
        kinds.add(kind.strip())
    return tuple(sorted(kinds))


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _model_map_block(text: str) -> list[str]:
    """Stripped content lines of the ``model_map:`` block in config text.

    Deliberately YAML-free: the gate runs inside whatever interpreter Hermes is
    using, and a missing PyYAML must not silently shrink the accepted routes to
    the built-in five. Stops at the first line that dedents out of the block and
    skips comments and blank lines.
    """
    lines: list[str] = []
    base: int | None = None
    for line in text.splitlines():
        if base is None:
            header = _MODEL_MAP_KEY.match(line)
            if header:
                base = len(header.group(1))
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= base:
            break
        lines.append(stripped)
    return lines


def _model_map_names(text: str) -> set[str]:
    """Route names under ``model_map.options``, parsed from config text."""
    names: set[str] = set()
    for stripped in _model_map_block(text):
        option = _OPTION_NAME.match(stripped)
        if option:
            names.add(option.group(1))
    return names


def _route_providers(text: str) -> dict[str, str]:
    """Map each ``model_map`` option name to its top-level ``provider:`` value.

    Only the option's own ``provider:`` key counts; ``- provider:`` entries under
    ``fallback_providers`` are list items and never match.
    """
    providers: dict[str, str] = {}
    current: str | None = None
    for stripped in _model_map_block(text):
        option = _OPTION_NAME.match(stripped)
        if option:
            current = option.group(1)
            continue
        if current is None or current in providers:
            continue
        value = _PROVIDER_LINE.match(stripped)
        if value:
            providers[current] = value.group(1)
    return providers


def tool_free_routes() -> frozenset[str]:
    """Route names whose provider is gemini-app (no filesystem, no shell)."""
    global _TOOL_FREE_CACHE
    if _TOOL_FREE_CACHE is not None:
        return _TOOL_FREE_CACHE
    try:
        with open(os.path.join(_hermes_home(), "config.yaml"), encoding="utf-8") as handle:
            providers = _route_providers(handle.read())
        free = frozenset(name for name, provider in providers.items() if provider == _TOOL_FREE_PROVIDER)
    except Exception:
        free = _TOOL_FREE_FALLBACK
    _TOOL_FREE_CACHE = free
    return free


def _directive_values(text: str, key: str) -> list[str]:
    """Values of every line beginning ``key:`` (markdown prefixes allowed)."""
    pattern = re.compile(_LEADING_MARKDOWN + re.escape(key) + r"\s*:\s*(.*)$", re.IGNORECASE)
    values: list[str] = []
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            values.append(match.group(1).strip())
    return values


def _artifact_tokens(text: str) -> list[str]:
    """Local-artifact tokens in order of first appearance, deduplicated."""
    found: list[tuple[int, str]] = []
    for pattern in _ARTIFACT_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(0).rstrip(".:")
            if token:
                found.append((match.start(), token))
    tokens: list[str] = []
    for _, token in sorted(found):
        if token not in tokens:
            tokens.append(token)
    return tokens


def _path_fault(raw: str) -> str | None:
    """Name the first reason a CONTEXT_PATH/CONTEXT_FOLDER argument is unusable."""
    value = raw.strip().strip("`'\"").strip()
    if not value:
        return "no path given"
    if not value.startswith("/"):
        return f"path {value!r} is not absolute"
    if not (value == _CONTEXT_ROOT or value.startswith(_CONTEXT_ROOT + "/")):
        return f"path {value} is outside {_CONTEXT_ROOT}"
    dotted = [segment for segment in value.split("/") if segment.startswith(".")]
    if dotted:
        return f"path {value} has dot-named segment {dotted[0]!r}"
    if not os.path.exists(value):
        return f"path {value} does not exist on disk"
    return None


def _tool_free_refusal(tasks: list[Any]) -> str | None:
    """Refusal text for tool-free tasks whose brief names local files without context."""
    free = tool_free_routes()
    problems: list[str] = []
    for index, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            continue
        option = task.get("option")
        if not isinstance(option, str) or option not in free:
            continue
        goal = task.get("goal")
        context = task.get("context")
        brief = f"{goal if isinstance(goal, str) else ''}\n{context if isinstance(context, str) else ''}"

        directives = _directive_values(brief, _CONTEXT_PATH_KEY) + _directive_values(brief, _CONTEXT_FOLDER_KEY)
        if directives:
            faults = [fault for fault in (_path_fault(raw) for raw in directives) if fault]
            if faults:
                problems.append(f"#{index} route '{option}': " + "; ".join(faults))
            continue
        # An opt-out needs a real reason: an empty or placeholder-only value (e.g. 'tbd')
        # falls through to the artifact check.
        if any(value and not is_placeholder_only(value) for value in _directive_values(brief, _CONTEXT_NONE_KEY)):
            continue
        tokens = _artifact_tokens(brief)
        if tokens:
            problems.append(
                f"#{index} route '{option}': the brief names local artifact(s) "
                f"{', '.join(tokens[:3])} but carries no context; this route's child is tool-free "
                "(no filesystem, no shell) and would answer from prose alone"
            )
    if not problems:
        return None
    return (
        "REFUSED [dispatch-envelope-gate]: tool-free route dispatch rejected.\n"
        + "\n".join(problems)
        + "\nRemedy: stage the source under your context root (set DISPATCH_CONTEXT_ROOT) and attach it with a CONTEXT_PATH: /abs/path "
        "or CONTEXT_FOLDER: /abs/path line, or declare a line 'CONTEXT_NONE: <reason>' if the brief "
        "needs no local file."
    )


def allowed_routes() -> frozenset[str]:
    """Valid `option` values: the built-ins plus any listed in
    ``$HERMES_HOME/dispatch-gate.json`` under ``"routes"``.

    Read once and cached, mirroring writer-route-gate's config override. A
    missing or malformed file falls back to the built-ins, so a config entry can
    only extend the list, never silently empty it. Without this, adding a route
    to model_map (e.g. an escalation coder) makes the gate reject a valid route.
    """
    global _ROUTES_CACHE
    if _ROUTES_CACHE is not None:
        return _ROUTES_CACHE
    routes = set(_ALLOWED_ROUTES)
    try:
        home = _hermes_home()
        with open(os.path.join(home, "dispatch-gate.json"), encoding="utf-8") as handle:
            extra = (json.load(handle) or {}).get("routes") or []
        routes.update(str(item).strip() for item in extra if str(item).strip())
    except Exception:
        pass
    try:
        with open(os.path.join(_hermes_home(), "config.yaml"), encoding="utf-8") as handle:
            routes.update(_model_map_names(handle.read()))
    except Exception:
        pass
    _ROUTES_CACHE = frozenset(routes)
    return _ROUTES_CACHE


def is_placeholder_only(value: Any) -> bool:
    """True when a field's entire value is placeholder text.

    Deliberately whole-value, not substring. A legitimate brief often mentions a
    TODO comment in the code under repair, or quotes a TBD from a source; a
    substring ban on "todo" anywhere in a long context would refuse real work.
    Only a field that is *nothing but* placeholder tokens is a scratch entry.
    """
    if not isinstance(value, str):
        return False
    if "lorem ipsum" in value.casefold():
        return True
    words = re.findall(r"[A-Za-z]+", value)
    if not words:
        return False
    return all(word.casefold() in _PLACEHOLDER_WORDS for word in words)


def _excerpt(value: Any, limit: int = 64) -> str:
    """Make a short, single-line identifier for a task in the repair message."""
    if not isinstance(value, str) or not value.strip():
        return "(goal unavailable)"
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "…"
    return compact.replace('"', "'")


def _block(message: str) -> dict[str, str]:
    """Build a valid host veto; an empty message would be ignored by Hermes."""
    return {"action": "block", "message": message or "Dispatch blocked: repair the task envelope and retry."}


def _section_values(context: str) -> dict[str, str]:
    """Return the first value for each envelope key found at the start of a line."""
    found: dict[str, str] = {}
    for key in _ENVELOPE_KEYS:
        pattern = re.compile(_LEADING_MARKDOWN + re.escape(key) + r"\s*:\s*(.*)$", re.IGNORECASE)
        for line in context.splitlines():
            match = pattern.match(line)
            if match:
                found[key] = match.group(1).strip()
                break
    return found


def _is_bare_writer_fact_negation(value: str) -> bool:
    """Recognize a bare no-facts answer, allowing punctuation around it."""
    normalized = re.sub(r"^[\W_]+|[\W_]+$", "", value.strip(), flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).casefold()
    return normalized in _WRITER_FACTS_NEGATIONS


def _waiver(context: str) -> tuple[bool, str | None]:
    """Return (valid waiver present, invalid-waiver diagnostic if found)."""
    pattern = re.compile(
        _LEADING_MARKDOWN + r"ENVELOPE\s*:\s*waived\b(.*)$", re.IGNORECASE
    )
    valid = False
    invalid: str | None = None
    for line in context.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        tail = match.group(1).strip()
        if tail.startswith("—") or tail.startswith("-"):
            reason = tail[1:].strip()
            if len(reason) >= 20:
                valid = True
            else:
                invalid = "brief.ENVELOPE: supply a waiver reason of at least 20 characters after 'waived —'"
        else:
            invalid = "brief.ENVELOPE: supply 'waived — <reason>' with a reason of at least 20 characters"
    return valid and invalid is None, invalid


def _steer_directive(args: dict[str, Any]) -> dict[str, str] | None:
    """Block steer text misused as a primary brief, but allow ordinary course-correction."""
    if args.get("action") != "steer":
        return None
    message = args.get("message")
    message = message if isinstance(message, str) else ""
    keys = {
        key
        for key in _ENVELOPE_KEYS
        if re.search(r"\b" + re.escape(key) + r"\s*:", message, re.IGNORECASE)
    }
    if len(keys) < 2 and len(message) <= 1500:
        return None
    reason = []
    if len(keys) >= 2:
        reason.append("it contains multiple primary-envelope section keys")
    if len(message) > 1500:
        reason.append(f"it is {len(message)} characters (the limit is 1500)")
    return _block(
        "Steer blocked: " + " and ".join(reason) + ". Steer text is appended to the child's "
        "next tool result, and a child that has already finished never receives it. Put the "
        "primary brief in context at dispatch; reserve steer for concise course-correction."
    )


def validate_dispatch(tool_name: Any, args: Any) -> dict[str, str] | None:
    """Validate a delegate_task call; return a non-empty block directive or None.

    All task defects are collected before the block is composed. Validation is
    intentionally conservative, deterministic, and bounded to in-memory data.
    """
    if tool_name != "delegate_task":
        return None
    if not isinstance(args, dict):
        return _block(
            "Dispatch blocked: args: supply an object containing a non-empty 'tasks' list; "
            f"received {type(args).__name__}.\n\n" + _skeleton()
        )

    steer = _steer_directive(args)
    if steer is not None:
        return steer
    if args.get("action") == "steer":
        return None
    if args.get("action") in ("list", "stop"):
        # Control actions carry no task batch: they inspect or end children that already
        # exist. Envelope validation must never block the calls needed to recover from a
        # bad dispatch.
        return None

    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        kind = "non-empty list" if isinstance(tasks, list) else "list"
        return _block(
            f"Dispatch blocked: tasks: supply a {kind} of task objects with goal, option, and a brief (in goal or context).\n\n"
            + _skeleton()
        )

    tool_free_refusal = _tool_free_refusal(tasks)
    if tool_free_refusal is not None:
        return _block(tool_free_refusal)

    errors: list[tuple[int, str, list[str]]] = []
    first_goals: dict[str, int] = {}
    for index, task in enumerate(tasks, 1):
        goal_value = task.get("goal") if isinstance(task, dict) else None
        excerpt = _excerpt(goal_value)
        violations: list[str] = []
        if not isinstance(task, dict):
            violations.append("tasks: supply a task object with goal, option, and a brief (in goal or context)")
        else:
            goal = task.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                violations.append("goal: supply a non-empty string of at least 15 characters")
            else:
                trimmed = goal.strip()
                if len(trimmed) < 15:
                    violations.append("goal: supply at least 15 non-whitespace characters")
                if not any(char.isalnum() for char in trimmed):
                    violations.append("goal: supply text, not only whitespace or punctuation")
                if is_placeholder_only(goal):
                    violations.append("goal: replace placeholder text with the real deliverable")
                normalized = re.sub(r"\s+", " ", trimmed).casefold()
                if normalized in first_goals:
                    violations.append(
                        f"goal: make this distinct from duplicate task #{first_goals[normalized]}"
                    )
                else:
                    first_goals[normalized] = index

            option = task.get("option")
            if not isinstance(option, str) or option not in allowed_routes():
                choices = ", ".join(sorted(allowed_routes()))
                violations.append(f"option: supply exactly one route: {choices}")

            context_value = task.get("context")
            context = context_value if isinstance(context_value, str) else ""
            # Validate one effective brief: goals are always present in dispatches,
            # while context is optional and may carry additional section values.
            effective_brief = f"{goal if isinstance(goal, str) else ''}\n{context}"
            if _LOREM_IPSUM.search(effective_brief):
                violations.append("brief: replace lorem ipsum placeholder text")
            values = _section_values(effective_brief)
            for key in _ENVELOPE_KEYS:
                if is_placeholder_only(values.get(key)):
                    violations.append(f"brief.{key}: replace placeholder text with the real value")

            if option == "writer" and (
                "FACTS" not in values
                or not values["FACTS"]
                or _is_bare_writer_fact_negation(values["FACTS"])
            ):
                violations.append(_WRITER_FACTS_VIOLATION)

            waived, waiver_error = _waiver(effective_brief)
            if waiver_error:
                violations.append(waiver_error)
            if not waived:
                for key in _ENVELOPE_KEYS:
                    field = f"brief.{key}"
                    if key not in values:
                        if not (option == "writer" and key == "FACTS"):
                            violations.append(f"{field}: add a line beginning '{key}:' with its value")
                    elif not values[key]:
                        if not (option == "writer" and key == "FACTS"):
                            violations.append(f"{field}: supply a non-empty value after '{key}:'")
                ceiling = values.get("CEILING")
                if ceiling and not re.search(r"\d", ceiling):
                    violations.append("brief.CEILING: include a numeric max-turn budget")
                brief_chars = len(effective_brief)
                approx_tokens = brief_chars // 4
                if brief_chars > _BRIEF_HARD_CHARS:
                    violations.append(
                        f"brief: {brief_chars} characters (~{approx_tokens:,} tokens) is over the "
                        f"{_BRIEF_HARD_CHARS}-character ceiling. Write the material to a file and cite "
                        "its path in PATHS: - the child reads it for itself at a fraction of the cost "
                        "of a pasted corpus. If the size is genuinely unavoidable, waive with "
                        "'ENVELOPE: waived - <reason of 20+ characters>'."
                    )
                elif brief_chars > _BRIEF_SOFT_CHARS and not _PATH_CITATION.search(effective_brief):
                    violations.append(
                        f"brief: {brief_chars} characters (~{approx_tokens:,} tokens) with no file path "
                        "cited. Pasting source material bills it at fresh-input rate in full; write it "
                        "to a file and put the path in PATHS: so the child reads what it needs."
                    )
        if violations:
            errors.append((index, excerpt, violations))

    if not errors:
        return None

    lines = ["Dispatch blocked: repair every listed task, then resend."]
    for index, goal, violations in errors:
        lines.append(f'#{index} goal="{goal}" — ' + "; ".join(violations))
    all_violations = [violation for _, _, violations in errors for violation in violations]
    key = _defect_kinds(all_violations)
    count = _REPEAT_COUNTS.get(key, 0) + 1
    _REPEAT_COUNTS[key] = count
    if len(_REPEAT_COUNTS) > _MAX_REPEAT_KEYS:
        _REPEAT_COUNTS.clear()
        _REPEAT_COUNTS[key] = count
    if count >= 2:
        lines.append(
            f"This is refusal #{count} for the same defect set. Rewording the goal is not a repair: "
            "the same defects are still present in the payload. Write the six labelled lines into goal."
        )

    lines.extend(
        [
            "",
            "Copy this six-line envelope into each task's goal (or its context) and fill it in:",
            "goal is emitted on every call and is the field that always arrives; context is optional and some models omit it.",
            _skeleton(),
        ]
    )
    return _block("\n".join(lines))


def _skeleton() -> str:
    """Copy-pasteable brief template appended to every repair directive."""
    return (
        "TASK: <deliverable>\n"
        "FACTS: <facts, none, or self-contained>\n"
        "PATHS: <exact file paths/URLs or n/a>\n"
        "OUTPUT: <what the return must contain>\n"
        "CHECK: <acceptance check>\n"
        "CEILING: <max turns, include a number>"
    )
