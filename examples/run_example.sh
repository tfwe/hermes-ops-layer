#!/usr/bin/env bash
# Runs both gate-catch demos offline and writes each case's stdout to examples/<case>.log.
# Exit 0 only when both gates catch their defect. Run from anywhere; paths are repo-relative.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
rc=0
for case in malformed-dispatch config-drift; do
  python3 "$HERE/demo_gates.py" "$case" > "$HERE/$case.log" || rc=$?
  cat "$HERE/$case.log"
  echo
done
exit "$rc"
