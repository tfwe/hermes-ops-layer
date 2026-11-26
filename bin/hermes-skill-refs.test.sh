#!/usr/bin/env bash
# Self-test for hermes-skill-refs.py. Builds temp homes; asserts line counts and exit codes.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/hermes-skill-refs.py"
BASE="${TMPDIR:-/tmp}"
WORK="$(mktemp -d "$BASE/skill-refs-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
fails=0

# mkhome NAME: creates $WORK/NAME/.hermes/skills/demo and $WORK/NAME/bin, prints the home dir
mkhome() {
  local h="$WORK/$1"
  mkdir -p "$h/.hermes/skills/demo" "$h/bin"
  echo "$h"
}

# skill HOME BODY: writes SKILL.md with BODY
skill() {
  printf '%s\n' "$2" > "$1/.hermes/skills/demo/SKILL.md"
}

# check_case NAME WANT_LINES WANT_EXIT ARGS...  (WANT_LINES includes the final count line)
check_case() {
  local name="$1" want_lines="$2" want_exit="$3"
  shift 3
  local out rc lines
  out="$("$SCRIPT" "$@" 2>"$WORK/err")"; rc=$?
  if [ -z "$out" ]; then lines=0; else lines=$(printf '%s\n' "$out" | wc -l); fi
  if [ "$lines" -eq "$want_lines" ] && [ "$rc" -eq "$want_exit" ]; then
    echo "PASS $name (lines=$lines exit=$rc)"
  else
    echo "FAIL $name: want lines=$want_lines exit=$want_exit, got lines=$lines exit=$rc"
    [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/    /'
    [ -s "$WORK/err" ] && sed 's/^/    stderr: /' "$WORK/err"
    fails=$((fails + 1))
  fi
}

# 1. good reference: existing executable and existing data file -> silent, exit 0
H=$(mkhome good)
printf '#!/bin/sh\n' > "$H/bin/tool.sh"; chmod 755 "$H/bin/tool.sh"
echo x > "$H/.hermes/notes.txt"
skill "$H" 'Run `~/bin/tool.sh --check` and read `~/.hermes/notes.txt`.'
check_case "good reference is silent" 0 0 --home "$H"

# 2. missing references, one under a directory that does not exist -> 2 lines, exit 1
H=$(mkhome missing)
skill "$H" 'Read `~/.hermes/gone.yaml` then `~/nodir/deep/x.py`.'
check_case "missing references reported (2 lines + count line)" 3 1 --home "$H"

# 3. placeholders only -> silent exit 0; --report counts 4 placeholders
H=$(mkhome placeholder)
skill "$H" 'Fill `~/.hermes/<name>/x`, `/opt/foo/*`, `~/projects/` and `~/{a,b}/c`.'
check_case "placeholders are skipped" 0 0 --home "$H"
out="$("$SCRIPT" --report --home "$H")"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | grep -qx 'placeholders: 4'; then
  echo "PASS report counts placeholders (exit=0)"
else
  echo "FAIL report placeholder count: exit=$rc out=$out"; fails=$((fails + 1))
fi

# 4. non-executable ~/bin script -> one NOT_EXECUTABLE line, exit 1
H=$(mkhome noexec)
printf '#!/bin/sh\n' > "$H/bin/tool.sh"; chmod 644 "$H/bin/tool.sh"
skill "$H" 'Call `~/bin/tool.sh` now.'
check_case "non-executable bin script reported (1 line + count line)" 2 1 --home "$H"
out="$("$SCRIPT" --home "$H")"
if printf '%s\n' "$out" | grep -q 'NOT_EXECUTABLE$'; then
  echo "PASS NOT_EXECUTABLE tag present"
else
  echo "FAIL NOT_EXECUTABLE tag missing: $out"; fails=$((fails + 1))
fi

# 5. allowlist glob suppresses a missing reference -> silent exit 0
H=$(mkhome allow)
skill "$H" 'Old path `~/.hermes/archive/old.md`.'
printf '# archived\n~/.hermes/archive/*\n' > "$H/.hermes/skills/.refs-allow"
check_case "allowlist suppresses match" 0 0 --home "$H"

# 6. report exits 0 even with a missing reference, with per-file counts
H=$(mkhome report)
skill "$H" 'Missing `~/nope.txt`.'
out="$("$SCRIPT" --report --home "$H")"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s\n' "$out" | grep -q 'refs=1 missing=1 not_executable=0'; then
  echo "PASS report exits 0 with per-file counts"
else
  echo "FAIL report: exit=$rc out=$out"; fails=$((fails + 1))
fi

# 7. no skills directory -> tool error, exit 2
H="$WORK/nothing"; mkdir -p "$H"
check_case "missing skills dir is tool error" 0 2 --home "$H"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; fi
echo "$fails FAILED"; exit 1
