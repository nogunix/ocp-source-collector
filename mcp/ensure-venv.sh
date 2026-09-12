#!/usr/bin/env bash
# ensure-venv.sh — rebuild mcp/.venv if it's missing.
#
# .venv is gitignored, so anything that wipes it (accidental `rm`, disk
# cleanup, etc.) leaves casket-mcp.service execing a nonexistent
# interpreter: systemd reports status=203/EXEC and Restart=on-failure
# retries every few seconds forever, silently. Wired in as this service's
# ExecStartPre so a missing venv self-heals instead of crash-looping.
#
# Idempotent: no-op when the venv already has a working python.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -x "$HERE/.venv/bin/python" ] && exit 0

echo "[ensure-venv] $HERE/.venv missing or broken, rebuilding..." >&2
python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
echo "[ensure-venv] rebuild complete" >&2
