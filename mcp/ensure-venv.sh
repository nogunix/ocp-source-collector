#!/usr/bin/env bash
# ensure-venv.sh — rebuild mcp/.venv if it's missing.
#
# .venv is gitignored, so anything that wipes it (accidental `rm`, disk
# cleanup, etc.) leaves casket-mcp.service execing a nonexistent
# interpreter: systemd reports status=203/EXEC and Restart=on-failure
# retries every few seconds forever, silently. Wired in as this service's
# ExecStartPre so a missing venv self-heals instead of crash-looping.
#
# "Working" means the server's dependency imports, not merely that the
# interpreter exists. The venv's python is a symlink to /usr/bin/python3, so
# a distro Python upgrade (Fedora 45: 3.14 -> 3.15, 2026-11-04) leaves it
# executable while every package sits in the old lib/python3.14/site-packages.
# The old `-x` check passed, casket_mcp.py died with "No module named 'mcp'",
# and the service crash-looped for a week (175k restarts) unnoticed.
#
# Idempotent: no-op when the venv's python can import the server class
# casket_mcp.py uses. That is also what makes a requirements bump (mcp 1.x ->
# 2.x) take effect: the old venv lacks mcp.server.mcpserver and is rebuilt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"

"$PY" -c 'import mcp.server.mcpserver' >/dev/null 2>&1 && exit 0

echo "[ensure-venv] $HERE/.venv missing or cannot import mcp, rebuilding..." >&2
# --clear: an upgraded-away lib/python3.N/ must not linger beside the new one.
python3 -m venv --clear "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
echo "[ensure-venv] rebuild complete" >&2
