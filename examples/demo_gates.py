#!/usr/bin/env python3
"""Offline demo: the gates CATCH a malformed dispatch and config drift.

Usage: demo_gates.py {malformed-dispatch|config-drift}

Stdlib plus PyYAML (the claim gate needs it). No network, no model calls, no
Hermes install. Each case prints its findings and a CAUGHT or MISSED verdict.
Exit 0 when the gate caught the defect, 1 when it did not.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE_PY = REPO / "plugins" / "dispatch-envelope-gate" / "gate.py"
CLAIM_GATE = REPO / "bin" / "hermes-claim-gate.py"

CONFIG = """_config_version: 53  # same route layout as bin/hermes-claim-gate.test.sh
delegation:
  provider: openrouter
  model: deepseek/deepseek-v4.1-flash
  backends:
    model_map:
      options:
        - name: coder
          provider: openrouter
          model: anthropic/claude-haiku-5.5
          description: Patch-sized code. Cheap.
        - name: repo-coder
          provider: openrouter
          model: google/gemini-4-hydrogen
          description: Escalation only. Expensive.
        - name: gemini-app
          provider: gemini-app
          model: gemini-app-neon
          description: Free reasoning shim.
"""

SOUL = """# Standing instructions
Dispatch code work to the coder route. Critique format: list the criticisms.

<!-- ROUTES:BEGIN -->
- `coder`: openrouter / `anthropic/claude-haiku-5.5`. Patch-sized code.
Default child (delegation.model): openrouter / `deepseek/deepseek-v4.1-flash`.
<!-- ROUTES:END -->
"""

MEMORY = """Hosts: example-host, user <USER>. Services bind the tailnet IP.
Persistence law: every persistent thing gets a zero-token check.
"""

USER = "<USER> wants blunt answers and precision over comfort.\n"


def verdict(caught: bool) -> str:
    return "CAUGHT" if caught else "MISSED"


def load_gate():
    spec = importlib.util.spec_from_file_location("dispatch_envelope_gate_demo", GATE_PY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {GATE_PY.relative_to(REPO)}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def malformed_dispatch() -> int:
    print("case: malformed dispatch (delegate_task)")
    print("gate: plugins/dispatch-envelope-gate/gate.py validate_dispatch")
    # Point the gate at a throwaway home with the fixture config, so the route
    # list in the output is machine-independent (never the local ~/.hermes).
    with tempfile.TemporaryDirectory(prefix="gate-demo-") as tmp:
        home = Path(tmp)
        (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
        os.environ["HERMES_HOME"] = str(home)
        try:
            gate = load_gate()
            # A placeholder goal with no option and no brief: the gate must refuse it.
            args = {"tasks": [{"goal": "placeholder"}]}
            print("input: tasks[1] = {'goal': 'placeholder'}")
            result = gate.validate_dispatch("delegate_task", args)
        finally:
            os.environ.pop("HERMES_HOME", None)
    caught = isinstance(result, dict) and result.get("action") == "block" and bool(result.get("message"))
    if caught:
        print(f"block action: {result['action']}")
        print("block message:")
        for line in result["message"].splitlines():
            print(f"  {line}")
    else:
        print(f"result: {result!r}")
    print(f"verdict: {verdict(caught)}")
    return 0 if caught else 1


def config_drift(work: Path) -> int:
    print("case: config drift (prompt file names a model absent from config.yaml)")
    print("gate: bin/hermes-claim-gate.py --home <throwaway home>")
    home = work / "fake-home"
    (home / "memories").mkdir(parents=True)
    (home / "skills").mkdir()
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (home / "SOUL.md").write_text(SOUL, encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text(MEMORY, encoding="utf-8")
    (home / "memories" / "USER.md").write_text(USER, encoding="utf-8")

    # Control: the untouched home must pass silently.
    control = subprocess.run(
        [sys.executable, str(CLAIM_GATE), "--home", str(home)],
        capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    print(f"control (config and prompts agree): exit {control.returncode}, stdout {len(control.stdout)} bytes")

    # Drift: SOUL.md outside the ROUTES block names a model config.yaml does not carry.
    with (home / "SOUL.md").open("a", encoding="utf-8") as f:
        f.write("Old notes: the reviewer used openai/gpt-4o before.\n")
    print("drift: appended to SOUL.md: 'Old notes: the reviewer used openai/gpt-4o before.'")
    run = subprocess.run(
        [sys.executable, str(CLAIM_GATE), "--home", str(home)],
        capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    sys.stdout.write("gate stdout:\n")
    for line in run.stdout.splitlines():
        sys.stdout.write(f"  {line}\n")
    if run.stderr.strip():
        sys.stdout.write("gate stderr:\n")
        for line in run.stderr.splitlines():
            sys.stdout.write(f"  {line}\n")
    print(f"gate exit: {run.returncode}")
    caught = run.returncode == 1 and "model id 'openai/gpt-4o'" in run.stdout
    print(f"verdict: {verdict(caught)}")
    return 0 if caught and control.returncode == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("malformed-dispatch", "config-drift"):
        print("usage: demo_gates.py {malformed-dispatch|config-drift}", file=sys.stderr)
        return 2
    if argv[1] == "malformed-dispatch":
        return malformed_dispatch()
    with tempfile.TemporaryDirectory(prefix="gate-demo-") as tmp:
        return config_drift(Path(tmp))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
