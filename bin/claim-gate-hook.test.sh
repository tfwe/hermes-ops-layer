#!/usr/bin/env bash
# Tests for the claim-gate-hook plugin. Calls the plugin's _hook handler directly (not through Hermes).
# Uses a scratch HERMES_HOME and a scratch log; never touches the real log or config.
# Prints PASS/FAIL per case and a final "N/M passed" line; exits nonzero if any case fails.
set -u
BIN="$(cd "$(dirname "$0")" && pwd)"
GATE="$BIN/hermes-claim-gate.py"
PLUGINS="${HERMES_PLUGINS_DIR:-/home/<USER>/.hermes/plugins}"
BASE="${TMPDIR:-/tmp}"
mkdir -p "$BASE"
ROOT="$(mktemp -d "$BASE/claimhook-test.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT

mkdir -p "$ROOT/home/memories"
cat > "$ROOT/home/config.yaml" <<'EOF'
_config_version: 53
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
EOF

export HERMES_HOME="$ROOT/home"
export CLAIM_GATE_LOG="$ROOT/claim-gate.jsonl"
export CLAIM_GATE_SCRIPT="$GATE"
unset CLAIM_GATE_ENFORCE

python3 - "$PLUGINS" "$ROOT" "$GATE" <<'PY'
import importlib, json, os, sys

plugin_parent, root, gate_script = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, plugin_parent)
mod = importlib.import_module("claim-gate-hook")   # the plugin package; _hook is its handler
gate = sys.modules["claim-gate-hook.gate"]
hook = mod._hook
LOG = os.environ["CLAIM_GATE_LOG"]

ESCALATION = "Escalate to repo-coder only after a coder run failed twice."
CLEAN = "Hosts: Pi hermes, user <USER>. Services bind the tailnet IP."

passed = total = 0

def check(name, ok, detail=""):
    global passed, total
    total += 1
    passed += bool(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))

def set_enforce(value):
    if value is None:
        os.environ.pop("CLAIM_GATE_ENFORCE", None)
    else:
        os.environ["CLAIM_GATE_ENFORCE"] = value

def reset_log():
    if os.path.exists(LOG):
        os.remove(LOG)

def log_lines():
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]

def mem(content, target="memory"):
    return {"target": target, "operations": [{"action": "add", "content": content}]}

def skill(new_string):
    return {"operations": [{"action": "patch", "name": "demo-skill", "old_string": "x", "new_string": new_string}]}

# A. clean memory write, dry-run (unset): allowed, exactly one allow line logged
set_enforce(None); reset_log()
r = hook("memory", mem(CLEAN))
lines = log_lines()
check("A clean memory, dry-run: allowed", r is None, repr(r))
check("A clean memory, dry-run: one allow line logged",
      len(lines) == 1 and lines[0].get("action") == "allow" and lines[0].get("mode") == "dry-run"
      and lines[0].get("verdict") == "clean", repr(lines))

# B. clean memory write, enforcing: allowed, nothing logged
set_enforce("1"); reset_log()
r = hook("memory", mem(CLEAN))
check("B clean memory, enforcing: allowed", r is None, repr(r))
check("B clean memory, enforcing: nothing logged", log_lines() == [], repr(log_lines()))

# C. escalation restated, enforcing: blocked, message names the rule, block line logged
set_enforce("1"); reset_log()
r = hook("memory", mem(ESCALATION))
lines = log_lines()
check("C escalation memory, enforcing: blocked",
      isinstance(r, dict) and r.get("action") == "block", repr(r))
check("C escalation memory, enforcing: message names the escalation rule",
      isinstance(r, dict) and "restates the escalation policy" in r.get("message", ""), repr(r))
check("C escalation memory, enforcing: one block line logged",
      len(lines) == 1 and lines[0].get("action") == "block", repr(lines))

# D. same content, dry-run (unset): allowed, logged as would-block
set_enforce(None); reset_log()
r = hook("memory", mem(ESCALATION))
lines = log_lines()
check("D escalation memory, dry-run: allowed", r is None, repr(r))
check("D escalation memory, dry-run: logged as would_block",
      len(lines) == 1 and lines[0].get("action") == "allow" and lines[0].get("would_block") is True
      and lines[0].get("violations"), repr(lines))

# E. CLAIM_GATE_ENFORCE set to something other than '1' is dry-run
set_enforce("0"); reset_log()
r = hook("memory", mem(ESCALATION))
check("E ENFORCE=0 behaves as dry-run (allowed, logged)",
      r is None and len(log_lines()) == 1 and log_lines()[0].get("would_block") is True, repr(log_lines()))

# F. unrelated tool: untouched, no log, even with violating text
set_enforce("1"); reset_log()
r = hook("read_file", {"path": "/tmp/x", "content": ESCALATION})
check("F unrelated tool: untouched", r is None and log_lines() == [], repr((r, log_lines())))

# G. skill_manage patch naming a route, enforcing: out of scope, untouched
set_enforce("1"); reset_log()
r = hook("skill_manage", skill("Use the coder route for this."))
check("G skill_manage patch naming a route, enforcing: untouched",
      r is None and log_lines() == [], repr((r, log_lines())))

# G2. flag file arms enforcement without any env var
set_enforce(None); reset_log()
flag = os.path.join(root, "state", "claim-gate-enforce")
os.makedirs(os.path.dirname(flag), exist_ok=True)
open(flag, "w").close()
os.environ["CLAIM_GATE_FLAG"] = flag
r = hook("memory", mem(ESCALATION))
check("G2 flag file arms: escalation blocked",
      isinstance(r, dict) and r.get("action") == "block", repr(r))
os.remove(flag)
r = hook("memory", mem(ESCALATION))
check("G3 flag file removed: dry-run again",
      r is None and any(l.get("would_block") for l in log_lines()), repr(log_lines()))
os.environ.pop("CLAIM_GATE_FLAG", None)
reset_log()

# H. gate script exits 2 (tool error), enforcing: fail open, error logged
fake = os.path.join(root, "fake-exit2.py")
with open(fake, "w") as fh:
    fh.write("import sys\nsys.stderr.write('boom')\nsys.exit(2)\n")
os.environ["CLAIM_GATE_SCRIPT"] = fake
set_enforce("1"); reset_log()
r = hook("memory", mem(ESCALATION))
lines = log_lines()
check("H gate exits 2, enforcing: fail open (allowed)", r is None, repr(r))
check("H gate exits 2: error logged", len(lines) == 1 and lines[0].get("verdict") == "error"
      and lines[0].get("action") == "allow", repr(lines))

# I. gate script missing, enforcing: fail open, error logged
os.environ["CLAIM_GATE_SCRIPT"] = os.path.join(root, "does-not-exist.py")
reset_log()
r = hook("memory", mem(ESCALATION))
lines = log_lines()
check("I gate script missing, enforcing: fail open (allowed)", r is None, repr(r))
check("I gate script missing: error logged", len(lines) == 1 and lines[0].get("verdict") == "error",
      repr(lines))
os.environ["CLAIM_GATE_SCRIPT"] = gate_script

# J. unexpected exception inside the gate: _hook fails open
real_run_check = gate.run_check
def boom(label, text):
    raise RuntimeError("simulated gate bug")
gate.run_check = boom
set_enforce("1"); reset_log()
try:
    r = hook("memory", mem(ESCALATION))
    check("J exception inside gate: fail open (allowed)", r is None, repr(r))
except Exception as exc:
    check("J exception inside gate: fail open (allowed)", False, f"raised {exc!r}")
finally:
    gate.run_check = real_run_check

print(f"{passed}/{total} passed")
sys.exit(0 if passed == total else 1)
PY
exit $?
