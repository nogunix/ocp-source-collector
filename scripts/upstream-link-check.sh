#!/bin/bash
# Host-side upstream link-health check: regenerate state/upstream-sources.tsv
# from the mounted caskets, then probe it with check-upstream-links.py.
#
# Why here and not in GitHub Actions: the manifest is derived from /srv, which
# only this host can see, and state/ is host-local (never committed to the
# public repo since 2026-09-12). Regenerating it on every run also removes the
# staleness problem a committed manifest had -- it always matches what is
# mounted right now. The Actions workflow stays as a no-op fallback.
#
# Designed to run from the upstream-link-check.timer systemd user unit.
# Also fine to run by hand; extra arguments go to check-upstream-links.py
# (e.g. --limit 20 for a smoke test).
#
# Exit: 0 ok / 1 NEW rot found (see the NEW ROT lines in the journal) or the
# export failed / 2 manifest missing.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# Same credential handling as casket-auto-update.sh: prefer an exported
# token, else borrow the gh CLI's. ~924 repos means ~924 API calls, far over
# the 60/h unauthenticated quota, so without a token most repos read "error".
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    if [[ -n "${GH_TOKEN:-}" ]]; then
        export GITHUB_TOKEN="$GH_TOKEN"
    elif command -v gh >/dev/null 2>&1; then
        GITHUB_TOKEN="$(gh auth token 2>/dev/null)" || true
        [[ -n "$GITHUB_TOKEN" ]] && export GITHUB_TOKEN
    fi
fi
[[ -n "${GITHUB_TOKEN:-}" ]] || log "WARNING: no GITHUB_TOKEN and no usable gh credential -- repo checks will hit the 60/h quota"

"$SCRIPT_DIR/export-upstream-sources.sh"
exec python3 "$SCRIPT_DIR/check-upstream-links.py" --jobs 8 "$@"
