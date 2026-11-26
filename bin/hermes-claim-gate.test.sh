#!/usr/bin/env bash
# Tests for hermes-claim-gate.py. Builds fake Hermes homes in a scratch dir; never touches the real one.
# Prints PASS/FAIL per case; exits nonzero if any case fails.
set -u
GATE="$(cd "$(dirname "$0")" && pwd)/hermes-claim-gate.py"
BASE="${TMPDIR:-/tmp}"
mkdir -p "$BASE"
ROOT="$(mktemp -d "$BASE/claimgate-test.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT
FAILS=0
TOTAL=0

CONFIG='_config_version: 53
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
'

SOUL_CLEAN='# Standing instructions
Dispatch code work to the coder route. Critique format: list the criticisms.

<!-- ROUTES:BEGIN -->
- `coder`: openrouter / `anthropic/claude-haiku-5.5`. Patch-sized code.
Default child (delegation.model): openrouter / `deepseek/deepseek-v4.1-flash`.
<!-- ROUTES:END -->
'

MEM_CLEAN='Hosts: Pi hermes, user <USER>. Services bind the tailnet IP.
Persistence law: every persistent thing gets a zero-token check.'

USER_CLEAN='<USER> wants blunt answers and precision over comfort.'

# mkhome NAME -> creates $ROOT/NAME with a clean baseline; prints its path
mkhome() {
  local h="$ROOT/$1"
  mkdir -p "$h/memories" "$h/skills"
  printf '%s' "$CONFIG" > "$h/config.yaml"
  printf '%s\n' "$SOUL_CLEAN" > "$h/SOUL.md"
  printf '%s\n' "$MEM_CLEAN" > "$h/memories/MEMORY.md"
  printf '%s\n' "$USER_CLEAN" > "$h/memories/USER.md"
  echo "$h"
}

# case NAME EXPECTED_EXIT "STDOUT_GREP_OR_EMPTY" -- gate args...
# STDOUT_GREP is a fixed string that must appear in stdout; "EMPTY" requires empty stdout.
run_case() {
  local name="$1" want_exit="$2" want_out="$3"; shift 3
  TOTAL=$((TOTAL + 1))
  local out rc
  out="$(python3 "$GATE" "$@" 2>"$ROOT/stderr.txt")"
  rc=$?
  local ok=1 why=""
  [ "$rc" -eq "$want_exit" ] || { ok=0; why="exit $rc, want $want_exit"; }
  if [ "$want_out" = "EMPTY" ]; then
    [ -z "$out" ] || { ok=0; why="$why; stdout not empty: $(printf '%s' "$out" | head -n 3 | tr '\n' ' ')"; }
  elif [ -n "$want_out" ]; then
    printf '%s' "$out" | grep -qF -- "$want_out" || { ok=0; why="$why; stdout lacks '$want_out'"; }
  fi
  if [ "$ok" -eq 1 ]; then
    echo "PASS $name"
  else
    echo "FAIL $name: $why"
    [ -s "$ROOT/stderr.txt" ] && head -n 2 "$ROOT/stderr.txt"
    FAILS=$((FAILS + 1))
  fi
}

# 1. clean home: exit 0, silent
H1=$(mkhome clean)
run_case "clean memory is silent and exits 0" 0 EMPTY --home "$H1"

# 2. dirty memory: the policy sentence the gate exists to refuse
H2=$(mkhome dirty)
printf '%s\n' 'Escalate to repo-coder only after coder failed twice on the same signature.' >> "$H2/memories/MEMORY.md"
run_case "dirty memory names file and exits 1" 1 "memories/MEMORY.md:" --home "$H2"
run_case "dirty memory line is labelled as violation" 1 "MEMORY.md:3: violation" --home "$H2"

# 3. route name and model id in USER.md (rule a)
H3=$(mkhome userroute)
printf '%s\n' 'Prefers the gemini-app route for reasoning.' >> "$H3/memories/USER.md"
run_case "route name in USER.md exits 1" 1 "memories/USER.md:2: violation: route name 'gemini-app'" --home "$H3"
H3b=$(mkhome userid)
printf '%s\n' 'Default model anthropic/claude-haiku-5.5 is cheap.' >> "$H3b/memories/USER.md"
run_case "model id in USER.md exits 1" 1 "model id 'anthropic/claude-haiku-5.5'" --home "$H3b"

# 4. SOUL outside the ROUTES block naming an unknown id (rule c)
H4=$(mkhome soulbad)
printf '%s\n' 'Old notes: the reviewer used openai/gpt-4o before.' >> "$H4/SOUL.md"
run_case "unknown model id in SOUL outside block exits 1" 1 "SOUL.md:" --home "$H4"
run_case "unknown id in SOUL names the id" 1 "model id 'openai/gpt-4o'" --home "$H4"

# 5. same unknown id inside the ROUTES block is not a claim outside it: skipped
H5=$(mkhome soulblock)
python3 - "$H5/SOUL.md" <<'PY'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read().replace(
    "- `coder`: openrouter", "- `ghost`: openrouter / `openai/gpt-4o`. retired\n- `coder`: openrouter")
open(p, "w", encoding="utf-8").write(t)
PY
run_case "unknown id inside ROUTES block is ignored" 0 EMPTY --home "$H5"
# 5b. prose with a slash (not model-shaped) is not a model id claim
H5b=$(mkhome prose)
printf '%s\n' 'Keep the firewall/network notes and device/ops runbooks separate.' >> "$H5b/SOUL.md"
run_case "prose slash tokens in SOUL are not model ids" 0 EMPTY --home "$H5b"

# 6. allowlist suppresses a line by path glob and line glob
H6=$(mkhome allow)
printf '%s\n' 'Escalate to repo-coder only after coder failed twice.' >> "$H6/memories/MEMORY.md"
printf '%s\n' 'memories/MEMORY.md:*Escalate to repo-coder*' > "$H6/skills/.claims-allow"
run_case "allowlist entry suppresses the line" 0 EMPTY --home "$H6"

# 7. skill with route reference: --report counts, exit 0 (skills are soft only)
H7=$(mkhome skill)
mkdir -p "$H7/skills/demo"
printf '%s\n' 'For patches use `coder` on anthropic/claude-haiku-5.5 and keep the diff small.' > "$H7/skills/demo/SKILL.md"
printf '%s\n' 'No route here.' > "$H7/skills/demo/notes.md"
run_case "skill route reference: --report exits 0 with counts" 0 \
  "soft: skill files naming a route or model id: 1 of 2; references: 2" --report --home "$H7"
run_case "skill route reference: default check is silent" 0 EMPTY --home "$H7"

# 8. --json: valid JSON, exit code matches, and nothing is written
H8=$(mkhome jsonhome)
printf '%s\n' 'Escalate to repo-coder only after coder failed twice.' >> "$H8/memories/MEMORY.md"
before="$(cd "$H8" && find . -type f -exec md5sum {} + | sort)"
out="$(python3 "$GATE" --json --home "$H8" 2>"$ROOT/stderr.txt")"; rc=$?
after="$(cd "$H8" && find . -type f -exec md5sum {} + | sort)"
TOTAL=$((TOTAL + 1))
if [ "$rc" -eq 1 ] && [ "$before" = "$after" ] && printf '%s' "$out" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["exit"]==1 and d["violations"] and d["soft"] is not None
' 2>/dev/null; then
  echo "PASS --json parses, exit 1, writes nothing"
else
  echo "FAIL --json: rc=$rc or JSON bad or files changed"
  FAILS=$((FAILS + 1))
fi

# 9. missing config is a tool error (exit 2), not a clean pass
H9="$ROOT/noconfig"; mkdir -p "$H9"
run_case "missing config.yaml exits 2" 2 "" --home "$H9"

echo "----"
if [ "$FAILS" -eq 0 ]; then
  echo "ALL PASS ($TOTAL cases)"
  exit 0
fi
echo "FAILED: $FAILS of $TOTAL cases"
exit 1
