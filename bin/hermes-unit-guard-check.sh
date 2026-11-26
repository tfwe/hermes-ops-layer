#!/bin/bash
# Zero-token health check for the hermes-unit-guard enforcement chain.
# Silent with exit 0 when healthy; otherwise prints one line naming the
# failure and exits 1. Never prompts (all root reads use sudo -n).
set -u

GUARD=/usr/local/lib/hermes-unit-guard.py
APPROVE=/usr/local/sbin/hermes-unit-approve
UNITS_DIR=/etc/systemd/system
MANIFEST=/var/lib/hermes-unit-guard/enforcement.sha256
LAST_RUN=/var/lib/hermes-unit-guard/last-run
MANAGED="$GUARD $APPROVE $UNITS_DIR/hermes-unit-guard.path $UNITS_DIR/hermes-unit-guard.service $UNITS_DIR/hermes-unit-guard.timer"

fail() { echo "hermes-unit-guard-check: $*"; exit 1; }

# 1. Units enabled and active
for unit in hermes-unit-guard.path hermes-unit-guard.timer; do
    systemctl is-enabled --quiet "$unit" 2>/dev/null || fail "$unit is not enabled"
    systemctl is-active --quiet "$unit" 2>/dev/null || fail "$unit is not active"
done

# 2. last-run present and younger than 24h
[ -f "$LAST_RUN" ] || fail "$LAST_RUN missing"
now=$(date +%s)
mtime=$(stat -c %Y "$LAST_RUN" 2>/dev/null) || fail "cannot stat $LAST_RUN"
[ $((now - mtime)) -lt 86400 ] || fail "last-run is older than 24h"

# 3. Guard self-test
"$GUARD" --self-test >/dev/null 2>&1 || fail "guard --self-test failed"

# 4. Guard integrity: root-owned, 0755
sudo -n true 2>/dev/null || fail "sudo -n unavailable (cannot read root-owned files)"
attrs=$(sudo -n stat -c '%U %G %a' "$GUARD" 2>/dev/null) || fail "cannot stat $GUARD via sudo -n"
[ "$attrs" = "root root 755" ] || fail "$GUARD is not root:root 0755 (is: $attrs)"

sha_of() { sudo -n sha256sum -- "$1" 2>/dev/null | awk '{print $1}'; }

# 5. Manifest: write on first run if absent, then verify every recorded sha
if ! sudo -n sh -c '[ -e "$1" ]' _ "$MANIFEST" 2>/dev/null; then
    tmp=$(mktemp) || fail "mktemp failed"
    for f in $MANAGED; do
        s=$(sha_of "$f")
        [ -n "$s" ] || { rm -f "$tmp"; fail "cannot read $f via sudo -n to build manifest"; }
        printf '%s  %s\n' "$s" "$f" >> "$tmp"
    done
    sudo -n install -o root -g root -m 0640 "$tmp" "$MANIFEST" || { rm -f "$tmp"; fail "cannot write $MANIFEST"; }
    rm -f "$tmp"
fi

manifest=$(sudo -n cat "$MANIFEST" 2>/dev/null) || fail "cannot read $MANIFEST via sudo -n"
for f in $MANAGED; do
    printf '%s\n' "$manifest" | awk -v p="$f" '$2 == p {found=1} END {exit !found}' \
        || fail "$f not covered by $MANIFEST"
done

while read -r want path; do
    [ -n "${path:-}" ] || continue
    got=$(sha_of "$path")
    [ -n "$got" ] || fail "cannot read $path via sudo -n"
    [ "$got" = "$want" ] || fail "sha256 mismatch: $path"
done <<< "$manifest"

exit 0
