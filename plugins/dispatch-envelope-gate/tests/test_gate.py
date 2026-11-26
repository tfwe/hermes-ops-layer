"""Regression and contract tests for the dispatch envelope gate."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import gate  # noqa: E402

ROOT = gate._CONTEXT_ROOT


GOOD_CONTEXT = """TASK: implement the requested feature
FACTS: Existing API behavior must remain compatible
PATHS: /repo/src/module.py and /repo/tests/test_module.py
CONTEXT_NONE: fixture paths are synthetic; the brief reads no local file
OUTPUT: Return the implementation summary and changed paths
CHECK: Run the focused test suite and report its result
CEILING: at most 4 turns, then stop and report
"""


def good_task(goal="Implement the validation behavior and tests"):
    return {"goal": goal, "option": "writer", "context": GOOD_CONTEXT}


def directive_for(args):
    return gate.validate_dispatch("delegate_task", args)


def assert_directive_shape(result):
    assert result is None or (
        isinstance(result, dict)
        and result.get("action") == "block"
        and isinstance(result.get("message"), str)
        and bool(result["message"].strip())
    )


def test_placeholder_goal_without_context_regression_is_blocked():
    result = directive_for({"tasks": [{"goal": "placeholder"}]})
    assert_directive_shape(result)
    assert result is not None
    assert "#1" in result["message"] and "placeholder" in result["message"].lower()


def test_proper_goal_writer_route_without_context_regression_is_blocked():
    result = directive_for({"tasks": [{"goal": "Write a clear integration guide", "option": "writer"}]})
    assert_directive_shape(result)
    assert result is not None
    assert "context" in result["message"]


def test_conformant_single_task_batch_passes():
    result = directive_for({"tasks": [good_task()]})
    assert_directive_shape(result)
    assert result is None


def test_writer_goal_with_full_envelope_and_no_context_passes():
    task = {"goal": GOOD_CONTEXT, "option": "writer"}
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_writer_goal_with_factless_facts_is_blocked():
    goal = GOOD_CONTEXT.replace(
        "FACTS: Existing API behavior must remain compatible",
        "FACTS: none",
    )
    result = directive_for({"tasks": [{"goal": goal, "option": "writer"}]})
    assert_directive_shape(result)
    assert result is not None
    assert 'the writer route must carry the facts to use' in result["message"]


def test_coder_goal_with_full_envelope_and_no_context_passes():
    task = {"goal": GOOD_CONTEXT, "option": "coder"}
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_missing_envelope_in_goal_and_context_is_blocked():
    task = {"goal": "Implement the parser and its regression tests", "option": "coder"}
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None
    assert "brief.TASK" in result["message"]


def test_writer_with_factless_facts_section_is_blocked():
    task = good_task()
    task["context"] = GOOD_CONTEXT.replace(
        "FACTS: Existing API behavior must remain compatible",
        "FACTS: none",
    )
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None
    assert (
        'brief.FACTS: the writer route must carry the facts to use; '
        'supply real facts or a source pointer, not "none"'
    ) in result["message"]


def test_writer_with_real_facts_passes():
    result = directive_for({"tasks": [good_task()]})
    assert_directive_shape(result)
    assert result is None


def test_coder_with_factless_facts_section_still_passes():
    task = good_task()
    task["option"] = "coder"
    task["context"] = GOOD_CONTEXT.replace(
        "FACTS: Existing API behavior must remain compatible",
        "FACTS: none",
    )
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_writer_rejects_missing_empty_and_punctuated_bare_facts():
    for facts in (None, "", "(N/A).", "'Self-contained!'", "-- no facts --", "N/A - the model knows."):
        task = good_task()
        if facts is None:
            task["context"] = "\n".join(
                line for line in GOOD_CONTEXT.splitlines() if not line.startswith("FACTS:")
            )
        else:
            task["context"] = GOOD_CONTEXT.replace(
                "FACTS: Existing API behavior must remain compatible",
                f"FACTS: {facts}",
            )
        result = directive_for({"tasks": [task]})
        assert_directive_shape(result)
        assert result is not None and 'brief.FACTS: the writer route must carry the facts to use;' in result["message"]


def test_mixed_batch_names_bad_task_index_and_does_not_silently_pass():
    result = directive_for({"tasks": [good_task(), {"goal": "placeholder", "option": "writer"}]})
    assert_directive_shape(result)
    assert result is not None
    assert "#2" in result["message"]
    assert "#1" not in result["message"]


def test_missing_route_is_blocked():
    result = directive_for({"tasks": [{"goal": "Implement a robust parser", "context": GOOD_CONTEXT}]})
    assert_directive_shape(result)
    assert result is not None and "option:" in result["message"]


def test_non_string_route_is_blocked_without_raising():
    task = good_task()
    task["option"] = ["writer"]
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None and "option:" in result["message"]


def test_legitimate_todo_mention_is_not_blocked():
    """A brief may reference TODO comments in the code under repair. A substring
    ban on 'todo' anywhere in a long context refuses real work (regression guard)."""
    result = directive_for({"tasks": [good_task("Implement TODO support for this endpoint")]})
    assert_directive_shape(result)
    assert result is None

    task = good_task()
    task["context"] = GOOD_CONTEXT + "FACTS: none - resolve the TODO comments in gate.py\n"
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_field_that_is_only_placeholder_text_is_blocked():
    for goal in ("TODO", "tbd", "xxx", "placeholder"):
        result = directive_for({"tasks": [{"goal": goal, "option": "coder", "context": GOOD_CONTEXT}]})
        assert_directive_shape(result)
        assert result is not None and "goal:" in result["message"]


def test_lorem_ipsum_anywhere_in_context_is_blocked():
    task = good_task()
    task["context"] = GOOD_CONTEXT + "Notes: lorem ipsum dolor sit amet\n"
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None and "brief:" in result["message"]


def test_route_list_is_extensible_from_config(monkeypatch, tmp_path):
    """Adding a route to model_map must not make the gate reject that route."""
    (tmp_path / "dispatch-gate.json").write_text('{"routes": ["coder-hard"]}')
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gate, "_ROUTES_CACHE", None)
    task = good_task()
    task["option"] = "coder-hard"
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None
    monkeypatch.setattr(gate, "_ROUTES_CACHE", None)


def test_route_list_includes_model_map_options_from_live_config(monkeypatch, tmp_path):
    """Names in delegation.backends.model_map.options are valid routes; retired names are not."""
    (tmp_path / "config.yaml").write_text(
        "delegation:\n"
        "  backends:\n"
        "    model_map:\n"
        "      options:\n"
        "        - name: repo-coder\n"
        "        - name: writer-internal\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gate, "_ROUTES_CACHE", None)
    routes = gate.allowed_routes()
    assert "repo-coder" in routes
    assert "writer-internal" in routes
    assert "hard-coder" not in routes
    monkeypatch.setattr(gate, "_ROUTES_CACHE", None)


def test_duplicate_goals_are_blocked_after_whitespace_normalization():
    result = directive_for({"tasks": [good_task("Implement this parser cleanly"), good_task("  IMPLEMENT   this parser cleanly ")]})
    assert_directive_shape(result)
    assert result is not None and "duplicate task #1" in result["message"]


def test_empty_tasks_list_is_blocked():
    result = directive_for({"tasks": []})
    assert_directive_shape(result)
    assert result is not None and "tasks:" in result["message"]


def test_valid_waiver_replaces_section_requirement():
    task = good_task()
    task["context"] = (
        "FACTS: This dispatch is self-contained but supplies the writer's usable facts.\n"
        "ENVELOPE: waived — this dispatch is fully self-contained and needs no supporting brief."
    )
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_waiver_without_reason_is_blocked():
    task = good_task()
    task["context"] = "ENVELOPE: waived"
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None and "brief.ENVELOPE" in result["message"]


def test_short_waiver_reason_is_blocked():
    task = good_task()
    task["context"] = "ENVELOPE: waived - too short"
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None and "brief.ENVELOPE" in result["message"]


def test_steer_with_multiple_envelope_keys_is_blocked_with_explanation():
    result = directive_for({"action": "steer", "message": "TASK: new deliverable\nCEILING: 3 turns"})
    assert_directive_shape(result)
    assert result is not None
    assert "next tool result" in result["message"]
    assert "already finished" in result["message"]
    assert "context at dispatch" in result["message"]


def test_steer_over_1500_characters_is_blocked():
    result = directive_for({"action": "steer", "message": "x" * 1501})
    assert_directive_shape(result)
    assert result is not None and "1500" in result["message"]


def test_ordinary_short_steer_is_not_rejected():
    result = directive_for({"action": "steer", "message": "Please check the edge case."})
    assert_directive_shape(result)
    assert result is None


def test_malformed_args_never_raise_and_return_valid_directives():
    for args in (None, "not an object", [good_task()]):
        result = gate.validate_dispatch("delegate_task", args)
        assert_directive_shape(result)
        assert result is not None


def test_non_delegate_tool_is_ignored():
    result = gate.validate_dispatch("read_file", {"tasks": []})
    assert_directive_shape(result)
    assert result is None


def test_markdown_prefixed_case_insensitive_sections_are_accepted():
    task = good_task()
    task["option"] = "coder"
    task["context"] = """  > **TASK: implement the behavior**
- facts: none
* PATHS: n/a
# OUTPUT: concise summary
  CHECK: all assertions pass
CEILING: at most 2 turns
"""
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_control_actions_are_not_blocked():
    """list/stop carry no task batch. Blocking them removes the only way out of a bad batch."""
    for args in ({"action": "list"}, {"action": "stop", "subagent_id": "sa-0-1"}):
        result = gate.validate_dispatch("delegate_task", args)
        assert_directive_shape(result)
        assert result is None


# --- config-derived route names ------------------------------------------------

_CONFIG_SAMPLE = """\
delegation:
  provider: hopscotch-openai
  backends:
    model_map:
      options:
        - name: coder
          description: 'Patch-sized code only.'
          provider: openrouter
          model: anthropic/claude-haiku-5.5
        - name: repo-coder
          description: >-
            Repo-scale code, spanning more than one file, whole-repo context.
          provider: openrouter
      # - name: commented-out
    other_key: 1
  model: openai-main/gpt-6-luna
"""


def test_model_map_names_parses_only_the_model_map_block():
    """A YAML-free parse must find real options, ignore comments, and stop at the dedent."""
    assert gate._model_map_names(_CONFIG_SAMPLE) == {"coder", "repo-coder"}


# --- brief size ceiling --------------------------------------------------------

FILLER = ("The corpus describes a retention policy, its exceptions and the review cadence. " * 40)


def sized_context(chars, paths="PATHS: n/a"):
    """A valid envelope padded to roughly `chars` characters."""
    body = GOOD_CONTEXT.replace("PATHS: /repo/src/module.py and /repo/tests/test_module.py", paths)
    pad = FILLER * max(1, chars // len(FILLER))
    return f"{body}PATHS: {paths}\n{pad[:chars]}"


def test_oversize_brief_is_blocked():
    """A pasted corpus bills at fresh-input rate; refuse it and name the file-offload fix."""
    ctx = sized_context(gate._BRIEF_HARD_CHARS + 5_000)
    result = directive_for({"tasks": [{"goal": "Review the pasted retention policy corpus and report gaps", "option": "writer", "context": ctx}]})
    assert_directive_shape(result)
    assert result is not None
    assert "characters" in result["message"]
    assert "file" in result["message"].lower()


def test_midsize_brief_without_a_path_is_blocked():
    """Over the soft limit with no path cited: the material should have been a file."""
    ctx = sized_context(gate._BRIEF_SOFT_CHARS + 5_000, paths="n/a")
    result = directive_for({"tasks": [{"goal": "Review the pasted retention policy corpus and report gaps", "option": "writer", "context": ctx}]})
    assert_directive_shape(result)
    assert result is not None
    assert "no file path" in result["message"]


def test_midsize_brief_citing_a_path_passes():
    """A big brief is acceptable when it carries a path the child reads for itself."""
    ctx = sized_context(gate._BRIEF_SOFT_CHARS + 5_000, paths=f"CONTEXT_PATH: {ROOT}/writer-voice-bundle")
    result = directive_for({"tasks": [{"goal": "Review the retention policy corpus and report gaps", "option": "writer", "context": ctx}]})
    assert_directive_shape(result)
    assert result is None


def test_oversize_brief_can_be_waived():
    """An explicit waiver with a real reason remains the escape hatch."""
    ctx = sized_context(gate._BRIEF_HARD_CHARS + 5_000).replace(
        "CEILING: at most 4 turns, then stop and report",
        "ENVELOPE: waived - the corpus exists only in this message, no file to cite\n"
        "CEILING: at most 4 turns, then stop and report",
    )
    result = directive_for({"tasks": [{"goal": "Review the pasted retention policy corpus and report gaps", "option": "writer", "context": ctx}]})
    assert_directive_shape(result)
    assert result is None


def test_envelope_less_coder_refusal_points_at_goal():
    """The repair skeleton must tell the coder to write the envelope into goal."""
    result = directive_for({"tasks": [{"goal": "Implement the parser and its regression tests", "option": "coder"}]})
    assert_directive_shape(result)
    assert result is not None
    assert "into each task's goal" in result["message"]


def test_repeated_defect_set_escalates_refusal_count():
    """Same defect set with reworded goals: the second refusal must name the repeat."""
    gate._REPEAT_COUNTS.clear()
    first = directive_for({"tasks": [{"goal": "Implement the parser and its regression tests", "option": "coder"}]})
    second = directive_for({"tasks": [{"goal": "Build the parser with regression coverage", "option": "coder"}]})
    assert first is not None and second is not None
    assert "This is refusal #2" not in first["message"]
    assert "This is refusal #2" in second["message"]
    assert "Rewording the goal is not a repair" in second["message"]
    gate._REPEAT_COUNTS.clear()


# --- tool-free route gate ------------------------------------------------------

GEMINI_CONTROL = """TASK: weigh a design trade-off
FACTS: Caching the rendered page lowers latency but raises invalidation cost
OUTPUT: A short recommendation with the main risk named
PATHS: n/a
CHECK: State the assumption the recommendation depends on
CEILING: at most 2 turns, then stop and report
"""


def tool_free_task(goal, option="writer", context=None):
    return {"goal": goal, "option": option, "context": context if context is not None else GOOD_CONTEXT}


def test_tool_free_routes_resolve_from_live_config():
    gate._TOOL_FREE_CACHE = None
    assert gate.tool_free_routes() == frozenset({"writer", "gemini-app"})


def test_tool_free_routes_resolve_from_config_text(monkeypatch, tmp_path):
    text = _CONFIG_SAMPLE.replace("      options:\n", "      options:\n        - name: chat\n          provider: gemini-app\n", 1)
    (tmp_path / "config.yaml").write_text(text)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    gate._TOOL_FREE_CACHE = None
    try:
        assert gate.tool_free_routes() == frozenset({"chat"})
    finally:
        gate._TOOL_FREE_CACHE = None


def test_tool_free_refusal_names_route_artifacts_and_remedy():
    goal = f"Review {ROOT}/.hermes/SOUL.md against config.yaml and gate.py and the notes.md file"
    result = directive_for({"tasks": [tool_free_task(goal, context="Summarise the findings")]})
    assert_directive_shape(result)
    message = result["message"]
    assert message.startswith("REFUSED [dispatch-envelope-gate]:")
    assert "route 'writer'" in message
    assert "CONTEXT_NONE" in message and ROOT in message
    listed = message.split("artifact(s) ", 1)[1].split(" but carries", 1)[0]
    assert len(listed.split(", ")) == 3


def test_tool_free_gemini_app_route_naming_local_file_is_refused():
    task = tool_free_task(f"Check {ROOT}/writer-voice-bundle/00-read-first.md for tone", option="gemini-app", context="TASK: check tone")
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None and "route 'gemini-app'" in result["message"]


def test_context_path_to_existing_writer_bundle_is_accepted():
    task = tool_free_task(
        "Draft the reply in the voice of the bundle",
        context=GOOD_CONTEXT + f"CONTEXT_PATH: {ROOT}/writer-voice-bundle\n",
    )
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is None


def test_context_folder_to_existing_directory_is_accepted():
    task = tool_free_task("Draft the reply in the voice of the bundle", context=GOOD_CONTEXT + f"CONTEXT_FOLDER: {ROOT}/writer-voice-bundle/\n")
    assert directive_for({"tasks": [task]}) is None


def test_context_path_faults_are_named():
    cases = {
        "CONTEXT_PATH: writer-voice-bundle": "not absolute",
        "CONTEXT_PATH: /etc/passwd": f"outside {ROOT}",
        f"CONTEXT_PATH: {ROOT}/.hermes/config.yaml": "dot-named segment",
        f"CONTEXT_PATH: {ROOT}/no-such-dir-xyz": "does not exist",
        "CONTEXT_PATH:": "no path given",
    }
    for line, fault in cases.items():
        result = directive_for({"tasks": [tool_free_task("Draft the reply", context=GOOD_CONTEXT + line + "\n")]})
        assert_directive_shape(result)
        assert result is not None and fault in result["message"], (line, result)
        assert result["message"].startswith("REFUSED [dispatch-envelope-gate]:")


def test_context_none_with_reason_is_accepted():
    task = tool_free_task("Summarise the reasoning in config.yaml", context=GOOD_CONTEXT + "CONTEXT_NONE: prose-only review, no file needed\n")
    assert directive_for({"tasks": [task]}) is None


def test_context_none_without_reason_does_not_opt_out():
    task = tool_free_task("Summarise the reasoning in config.yaml", context="TASK: summarise\nFACTS: none\nPATHS: n/a\nOUTPUT: summary\nCHECK: n/a\nCEILING: 2 turns\nCONTEXT_NONE:\n")
    result = directive_for({"tasks": [task]})
    assert_directive_shape(result)
    assert result is not None and "config.yaml" in result["message"]


def test_gemini_app_design_trade_off_without_local_file_passes():
    """Control: abstract reasoning on a tool-free route must not be refused."""
    task = {"goal": "Weigh caching against freshness for the page", "option": "gemini-app", "context": GEMINI_CONTROL}
    assert directive_for({"tasks": [task]}) is None


def test_bare_words_and_abstract_terms_are_not_artifacts():
    assert gate._artifact_tokens("Discuss the trade-off between latency, cost and config drift in a design review.") == []


def test_non_tool_free_route_is_not_refused_by_this_rule():
    task = tool_free_task(f"Patch the loader in {ROOT}/.hermes/plugins/dispatch-envelope-gate/gate.py", option="coder", context=GOOD_CONTEXT)
    assert directive_for({"tasks": [task]}) is None


GEMINI_CONFIG_BRIEF = "Summarise the tone rules in config.yaml for the reply"


def _gemini_config_task(context_none_value):
    base = "".join(line + "\n" for line in GOOD_CONTEXT.splitlines() if not line.startswith("CONTEXT_NONE"))
    context = base + f"CONTEXT_NONE: {context_none_value}\n"
    return tool_free_task(GEMINI_CONFIG_BRIEF, option="gemini-app", context=context)


def test_context_none_empty_reason_on_gemini_app_is_refused():
    result = directive_for({"tasks": [_gemini_config_task("")]})
    assert_directive_shape(result)
    assert result is not None and "config.yaml" in result["message"]
    assert result["message"].startswith("REFUSED [dispatch-envelope-gate]:")


def test_context_none_whitespace_reason_on_gemini_app_is_refused():
    result = directive_for({"tasks": [_gemini_config_task("   ")]})
    assert_directive_shape(result)
    assert result is not None and "config.yaml" in result["message"]
    assert result["message"].startswith("REFUSED [dispatch-envelope-gate]:")


def test_context_none_tbd_placeholder_on_gemini_app_is_refused():
    result = directive_for({"tasks": [_gemini_config_task("tbd")]})
    assert_directive_shape(result)
    assert result is not None and "config.yaml" in result["message"]
    assert result["message"].startswith("REFUSED [dispatch-envelope-gate]:")


def test_context_none_real_reason_on_gemini_app_is_accepted():
    result = directive_for({"tasks": [_gemini_config_task("the config text is pasted into FACTS")]})
    assert_directive_shape(result)
    assert result is None
