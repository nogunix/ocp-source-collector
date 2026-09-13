#!/bin/bash
# Tests for the filesystem-type filter in scripts/casket-mounts.sh.
# The real script reads /proc/mounts; here we feed synthetic lines to the same
# filter expression and verify squashfs, erofs, and other types are handled.
#
# Run: bash tests/test_casket_mounts_fstype.sh   (exit 0 = pass)
set -uo pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

# The filter under test (extracted from casket-mounts.sh):
#   [[ ( "$fstype" == "squashfs" || "$fstype" == "erofs" ) && "$mnt" == /srv/sources-* ]]
accepts() {
    local dev="$1" mnt="$2" fstype="$3"
    [[ ( "$fstype" == "squashfs" || "$fstype" == "erofs" ) && "$mnt" == /srv/sources-* ]]
}

# ---- squashfs under /srv/sources-* -> accepted --------------------------------
accepts /dev/loop0 /srv/sources-ocp4.20.22 squashfs \
    && pass "squashfs under /srv/sources-* accepted" \
    || bad  "squashfs under /srv/sources-* accepted"

# ---- erofs under /srv/sources-* -> accepted (new behaviour) -------------------
accepts /dev/loop1 /srv/sources-ocp4.22.1 erofs \
    && pass "erofs under /srv/sources-* accepted" \
    || bad  "erofs under /srv/sources-* accepted"

# ---- ext4 under /srv/sources-* -> rejected ------------------------------------
accepts /dev/sda1 /srv/sources-ocp4.20.22 ext4 \
    && bad  "ext4 under /srv/sources-* rejected" \
    || pass "ext4 under /srv/sources-* rejected"

# ---- squashfs outside /srv/sources-* -> rejected ------------------------------
accepts /dev/loop2 /mnt/data squashfs \
    && bad  "squashfs outside /srv/sources-* rejected" \
    || pass "squashfs outside /srv/sources-* rejected"

# ---- erofs outside /srv/sources-* -> rejected ---------------------------------
accepts /dev/loop3 /opt/caskets erofs \
    && bad  "erofs outside /srv/sources-* rejected" \
    || pass "erofs outside /srv/sources-* rejected"

# ---- tmpfs under /srv/sources-* -> rejected -----------------------------------
accepts tmpfs /srv/sources-overlay tmpfs \
    && bad  "tmpfs under /srv/sources-* rejected" \
    || pass "tmpfs under /srv/sources-* rejected"

# ---- integration: feed synthetic /proc/mounts lines ---------------------------
# Simulate the while-read loop from casket-mounts.sh against mixed input.
declare -A HAVE
while read -r dev mnt fstype _; do
    [[ ( "$fstype" == "squashfs" || "$fstype" == "erofs" ) && "$mnt" == /srv/sources-* ]] || continue
    HAVE["$mnt"]="$dev"
done <<'MOUNTS'
/dev/loop0 /srv/sources-ocp4.18.48 squashfs ro,relatime 0 0
/dev/loop1 /srv/sources-ocp4.20.29 erofs ro,relatime 0 0
/dev/loop2 /srv/sources-ocp4.22.5 erofs ro,relatime 0 0
/dev/sda1 / ext4 rw,relatime 0 0
tmpfs /tmp tmpfs rw 0 0
/dev/loop5 /mnt/other squashfs ro 0 0
MOUNTS

[[ ${#HAVE[@]} -eq 3 ]] \
    && pass "mixed input: 3 casket mounts found (2 erofs + 1 squashfs)" \
    || bad  "mixed input: expected 3, got ${#HAVE[@]}"

[[ -n "${HAVE[/srv/sources-ocp4.18.48]:-}" ]] \
    && pass "mixed input: squashfs mount present" \
    || bad  "mixed input: squashfs mount present"

[[ -n "${HAVE[/srv/sources-ocp4.20.29]:-}" ]] \
    && pass "mixed input: erofs mount 4.20 present" \
    || bad  "mixed input: erofs mount 4.20 present"

[[ -n "${HAVE[/srv/sources-ocp4.22.5]:-}" ]] \
    && pass "mixed input: erofs mount 4.22 present" \
    || bad  "mixed input: erofs mount 4.22 present"

[[ -z "${HAVE[/mnt/other]:-}" ]] \
    && pass "mixed input: non-/srv mount excluded" \
    || bad  "mixed input: non-/srv mount excluded"

# ---- done --------------------------------------------------------------------
if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
