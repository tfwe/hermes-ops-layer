#!/usr/bin/env bash
# Runs both gate-catch demos and the real-world failure suite offline, writing
# each case's stdout to examples/<case>.log. Exit 0 only when every gate catches
# its defect. Run from anywhere; paths are repo-relative.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
rc=0
for case in malformed-dispatch config-drift; do
  python3 "$HERE/demo_gates.py" "$case" > "$HERE/$case.log" || rc=$?
  cat "$HERE/$case.log"
  echo
done
python3 "$HERE/real_world_cases.py" > "$HERE/real-world.log" || rc=$?
cat "$HERE/real-world.log"
exit "$rc"
