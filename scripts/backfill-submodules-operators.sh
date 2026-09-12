#!/usr/bin/env bash
# One-off driver: backfill expanded git submodules into the five Phase B
# operators caskets that carry ZTWIM (4.18-4.22), producing NEW .sqfs.xz
# artifacts. It does NOT touch fstab, mounts or the registry -- swapping is a
# separate, deliberate step.
#
# Usage (kill-resistant, per the systemd-run lesson in CLAUDE.md):
#   systemd-run --user --unit=casket-backfill-sm --collect \
#     %h/ocp-source-collector/scripts/backfill-submodules-operators.sh
# Read progress from the log named below (also on the user journal).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# CASKET_WORK defaults to THIS checkout (parent of scripts/), never $HOME:
# the repo directory is renameable, and under sudo $HOME is /root.
: "${CASKET_WORK:=$(dirname "$SCRIPT_DIR")}"
OUT_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
DATE="$(date -u +%Y%m%d)"
LOG="$CASKET_WORK/backfill-submodules-$DATE.log"

# A user unit starts from a minimal environment, so every one of these has to
# be set explicitly -- PATH for gh/mksquashfs, the store because the fallback
# lands in the repo, and the token because without it the trees API rate limit
# (60/h) silently downgrades every row to a branch head.
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/bin"
# Store and repack upper dir MUST share a filesystem: staging is hardlinks.
export CASKET_SUBMODULE_STORE="${CASKET_SUBMODULE_STORE:-$CASKET_WORK/submodule-store}"
export CASKET_REPACK_TMP="${CASKET_REPACK_TMP:-$CASKET_WORK/.repack-tmp}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null)}"

exec > >(tee -a "$LOG") 2>&1
say() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

[[ -n "$GITHUB_TOKEN" ]] || say "WARNING: no GITHUB_TOKEN — expect exact=0 (APPROX) rows"
say "store=$CASKET_SUBMODULE_STORE tmp=$CASKET_REPACK_TMP out=$OUT_DIR"

rc=0
for v in 4.18 4.19 4.20 4.21 4.22; do
    m="/srv/sources-ocp$v-operators"
    o="$OUT_DIR/casket-$DATE-ocp$v-operators.sqfs.xz"
    if [[ ! -d "$m/git" ]]; then say "SKIP $v: $m/git missing"; continue; fi
    say "=== $v -> $o"
    t0=$SECONDS
    if "$SCRIPT_DIR/repackage-add-submodules.sh" -m "$m" -o "$o" -f B -j 8; then
        say "=== $v done in $((SECONDS - t0))s ($(du -h "$o" 2>/dev/null | cut -f1))"
    else
        say "=== $v FAILED (rc=$?) after $((SECONDS - t0))s — continuing with the rest"
        rc=1
    fi
    df -h / | tail -1
done
say "all done (rc=$rc)"
exit $rc
