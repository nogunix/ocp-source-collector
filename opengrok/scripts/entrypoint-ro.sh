#!/usr/bin/env bash
#
# Custom OpenGrok container entrypoint for casket-ocp.
#
# The stock /scripts/entrypoint.sh runs `chown -R appuser:appgroup` over the
# whole source root. casket-ocp mounts the source from read-only squashfs, so
# that chown fails and (under `set -e`) aborts startup. The source trees are
# already world-readable, so appuser can index them without any ownership fix.
#
# This wrapper performs only the writable chowns (tomcat webapps, /opengrok/etc,
# /opengrok/data) and then drops to appuser to run the real start program.
#
set -euo pipefail

OWNER_USER=appuser
OWNER_GROUP=appgroup

chown -R "$OWNER_USER:$OWNER_GROUP" /usr/local/tomcat/webapps
chown -R "$OWNER_USER:$OWNER_GROUP" /opengrok/etc

# Index/data volume must be writable by appuser. A recursive chown over a large
# pre-populated index (~215G here) takes many minutes and was being paid on
# EVERY start, delaying REST availability. It is only needed when the volume
# isn't already appuser-owned: a fresh volume (root-owned by podman) or one
# pre-populated as root by the air-gap deploy. On normal restarts the tree is
# already appuser-owned (OpenGrok wrote it), so skip the -R and just fix the top
# dir cheaply. Force a full pass with FORCE_DATA_CHOWN=1 if ownership drifted.
if [ "${FORCE_DATA_CHOWN:-0}" = "1" ] || [ "$(stat -c %U /opengrok/data 2>/dev/null)" != "$OWNER_USER" ]; then
    echo "[entrypoint-ro] chowning /opengrok/data -> $OWNER_USER (fresh/pre-populated volume; one-time)…"
    chown -R "$OWNER_USER:$OWNER_GROUP" /opengrok/data
else
    echo "[entrypoint-ro] /opengrok/data already owned by $OWNER_USER; skipping recursive chown"
    chown "$OWNER_USER:$OWNER_GROUP" /opengrok/data 2>/dev/null || true
fi

# Try to chown only the source root directory itself (NOT -R): OpenGrok's ctags
# validation writes a probe temp file directly under /opengrok/src. This is
# best-effort — when the staging tree is bind-mounted read-only directly at
# /opengrok/src the chown fails, but the probe failure is only a cosmetic
# warning and indexing proceeds normally. The per-project subdirectories are
# read-only squashfs mounts and are left alone regardless.
chown "$OWNER_USER:$OWNER_GROUP" /opengrok/src 2>/dev/null || true

exec gosu "$OWNER_USER" /scripts/start.py "$@"
