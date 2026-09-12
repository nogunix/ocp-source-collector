#!/usr/bin/env bash
#
# run-opengrok.sh — (re)stage casket-ocp sources and launch the OpenGrok
# source-code browser container against them.
#
# What it does:
#   1. runs stage-sources.sh to (re)build the clean symlink tree at $STAGE_DIR
#      (only minors listed in config/opengrok-minors.txt are staged)
#   2. starts the official opengrok/docker container with:
#      - the staging tree bind-mounted read-only at /opengrok/src
#      - only the /srv/sources-* mounts referenced by the staging tree
#        bind-mounted at their real paths (so the staging symlinks resolve
#        inside the container)
#      - --canonicalRoot /srv/ so OpenGrok follows those symlinks
#      - -i <pattern> for every entry in config/opengrok-ignore.txt (subtrees
#        the indexer must not enter — chiefly symlink cycles, which make its
#        post-index cleanup spin forever; see find-symlink-cycles.py)
#      - --disableRepository Perforce,git,CVS (extracted trees have no SCM we
#        care about; the p4 probe otherwise hangs 8s per directory, and embedded
#        pseudo-git dirs — ko-build kodata/HEAD, SRPM-bundled libssh .git —
#        get mis-detected as git repos whose HEAD won't resolve, which aborts
#        the whole project's indexing with a history-cache SEVERE. We index
#        snapshots/tarballs, so no SCM history is wanted anyway.
#        CVS joined the list on 2026-09-12: the RHEL telnet SRPM ships a 1998
#        checkout of netkit-telnet with telnet/CVS and libtelnet/CVS intact
#        (Root = :ext:jbj@devserv:/mnt/devel/CVS, an internal host long gone),
#        so both srpms-*-extensions projects listed two unreachable CVS
#        repositories on the search page — cosmetic, but the container does
#        carry /usr/bin/cvs, so leaving them registered invites a probe of a
#        host that cannot answer.)
#      - --security-opt label=disable (read-only squashfs is SELinux user_home_t
#        and cannot be relabeled for container access)
#      - a custom entrypoint that skips the stock chown -R of the read-only src
#
# Indexing starts automatically once the webapp is up and runs in the background
# inside the container. Watch progress with: podman logs -f $CONTAINER
#
# Env overrides:
#   CONTAINER          container name           (default casket-ocp-grok)
#   IMAGE              image ref                (default docker.io/opengrok/docker:latest)
#   PORT               host port for :8080      (default 8080)
#   STAGE_DIR          staging symlink tree     (default /srv/opengrok-src)
#   SRV_ROOT           casket mount root        (default /srv)
#   DATA_VOLUME        index location: bind dir or named volume
#                      (default /srv/opengrok-data — SSD for performance)
#   INDEXER_JAVA_OPTS  indexer JVM opts         (default -Xmx8g; use -Xmx2g on a
#                      RAM-bound host — the indexer starts one JVM per project in
#                      parallel and 8g x ~25 exhausts RAM -> pthread EAGAIN)
#   CATALINA_OPTS      webapp JVM opts          (default -Xmx24g; the empty
#                      default let the ~25%-of-RAM heap wedge the webapp in a GC
#                      death-spiral — Tomcat alive but not listening on 8080 —
#                      when registering the full-catalog + container-RPM indexes,
#                      2026-07-19. Raise toward -Xmx32g if it recurs.)
#   WORKERS            parallel project-sync workers (default: image default =
#                      nproc, i.e. 16 on casket-host). Every container recreate
#                      (any run-opengrok.sh invocation, and every host reboot)
#                      re-syncs all ~25 projects and runs up to WORKERS of them
#                      concurrently, each spawning its own JVM — that's what
#                      makes the host heavy right after (re)start. Lower this
#                      (e.g. 4) to trade a longer total resync for a much lower
#                      peak CPU/RAM footprint. This script defaults it to 4.
#   SKIP_STAGE=1       reuse existing staging tree (don't rebuild)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${CONFIG_DIR:-$HERE/../../config}"

CONTAINER="${CONTAINER:-casket-ocp-grok}"
# Pin the exact image the prebuilt index was built against. A different image
# trips OpenGrok's check_index_and_wipe_out and discards the 160G index, so we
# prefer the digest when it is present locally (build host: guards against a
# `latest` tag that drifts after a future `podman pull`). On an air-gapped host,
# `podman load` of the saved tag usually does NOT carry the registry digest, so
# fall back to the :latest tag there. Override with IMAGE=... if needed.
IMAGE_DIGEST="${IMAGE_DIGEST:-docker.io/opengrok/docker@sha256:5bcdde2e8f9e52990817c2bb5be6532e9ed1a83ef883d9d36a25277aee403206}"
IMAGE_TAG="${IMAGE_TAG:-docker.io/opengrok/docker:latest}"
if [ -z "${IMAGE:-}" ]; then
  if podman image exists "$IMAGE_DIGEST" 2>/dev/null; then IMAGE="$IMAGE_DIGEST"; else IMAGE="$IMAGE_TAG"; fi
fi
PORT="${PORT:-8080}"
STAGE_DIR="${STAGE_DIR:-/srv/opengrok-src}"
SRV_ROOT="${SRV_ROOT:-/srv}"
# Index MUST live on SSD. HDD (tried at /mnt/hdd/opengrok-data) is not viable:
# xref stores one tiny .gz per source file, so the index is millions of inodes
# with random-access IO — inode-bound, not size-bound. On HDD both indexing and
# search become unusably slow. Footprint with only the config/opengrok-minors.txt
# minors indexed is ~150-250G, which fits comfortably on the root SSD.
DATA_VOLUME="${DATA_VOLUME:-/srv/opengrok-data}"
# Persist /opengrok/etc (configuration.xml) across container recreates.
ETC_VOLUME="${ETC_VOLUME:-/srv/opengrok-etc}"
INDEXER_JAVA_OPTS="${INDEXER_JAVA_OPTS:--Xmx8g}"
WORKERS="${WORKERS:-4}"

mkdir -p "$DATA_VOLUME" "$ETC_VOLUME"

log() { printf '[run-opengrok] %s\n' "$*" >&2; }

# Indexer ignore patterns (config/opengrok-ignore.txt), one `-i <pat>` each.
# These are not cosmetic: a symlink cycle inside an indexed tree makes
# OpenGrok's post-index cleanup recurse forever (see that file's header and
# find-symlink-cycles.py). Missing file = no ignores, which is only a
# performance/robustness regression, so don't make it fatal.
IGNORE_FILE="${IGNORE_FILE:-$CONFIG_DIR/opengrok-ignore.txt}"
IGNORE_OPT=""
if [ -r "$IGNORE_FILE" ]; then
  n_ignore=0
  while IFS= read -r pat; do
    pat="${pat%%#*}"
    pat="$(printf '%s' "$pat" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    [ -z "$pat" ] && continue
    IGNORE_OPT="$IGNORE_OPT -i $pat"
    n_ignore=$((n_ignore + 1))
  done < "$IGNORE_FILE"
  log "indexer ignore patterns: $n_ignore (from $(basename "$IGNORE_FILE"))"
else
  log "WARNING: $IGNORE_FILE not found — indexing with no ignore patterns"
fi

# 1. (re)build the staging symlink tree.
if [ "${SKIP_STAGE:-0}" != "1" ]; then
  log "staging sources ($STAGE_DIR) ..."
  "$HERE/stage-sources.sh"
else
  log "SKIP_STAGE=1: reusing existing $STAGE_DIR"
fi
[ -d "$STAGE_DIR" ] || { echo "staging dir $STAGE_DIR missing" >&2; exit 1; }

# 2. Collect bind mounts — only the /srv/sources-* dirs that the staging tree
#    actually links into (readlink every symlink, extract the mount path).
#    This avoids mounting 48 caskets when only ~15 are indexed.
mounts=( -v "$STAGE_DIR:/opengrok/src:ro" )
declare -A seen_mounts
while IFS= read -r link; do
  target="$(readlink "$link" 2>/dev/null)" || continue
  # Extract the /srv/sources-<something> mount path (first 3 path components).
  # e.g. /srv/sources-ocp4.20.27/git/etcd-... -> /srv/sources-ocp4.20.27
  mount_path="$(printf '%s' "$target" | sed -E 's|^(/srv/sources-[^/]+).*|\1|')"
  [ -d "$mount_path" ] || continue
  [ "${seen_mounts[$mount_path]+_}" ] && continue
  seen_mounts["$mount_path"]=1
  mounts+=( -v "$mount_path:$mount_path:ro" )
done < <(find "$STAGE_DIR" -maxdepth 3 -type l 2>/dev/null)
log "${#seen_mounts[@]} source mounts needed"

# ~/.config/systemd/user/opengrok.service keeps the container running across
# reboots with `podman start -a <name>` + Restart=always. It deliberately does
# not CREATE the container -- this script does -- but recreating one under it
# starts a fight: the unit's attached start dies with the old container, comes
# back 10s later, and fails forever against the new one (which `podman run -d`
# already started), because `podman start` wants a Created/Stopped container.
# That loop ran from 2026-07-31 09:56 to 2026-08-02 23:01 -- 21,321 journal
# entries, NRestarts=21452 -- and its churn tears the rootless pasta port
# forwarder down minutes after startup, which presents as "Tomcat answers
# inside the container, host gets connection-refused" (see 2b below; the
# startup check passes, then the port quietly goes away).
#
# So: stop the unit around the recreate, and hand the container back to it
# afterwards. Guarded by `unit_active` because this script must keep working on
# a host where the unit was never installed.
OPENGROK_UNIT="${OPENGROK_UNIT:-opengrok.service}"
unit_installed() { systemctl --user cat "$OPENGROK_UNIT" >/dev/null 2>&1; }
if unit_installed && [ "$(systemctl --user is-active "$OPENGROK_UNIT" 2>/dev/null)" = "active" ]; then
  log "stopping $OPENGROK_UNIT while the container is recreated"
  systemctl --user stop "$OPENGROK_UNIT" >/dev/null 2>&1 || true
fi

log "replacing any existing container '$CONTAINER'"
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

# `create`, not `run -d`: the unit's job is to RUN the container (`podman start
# -a` + Restart=always), and handing it an already-running one is exactly the
# fight described above. Creating it stopped lets the unit own the process,
# which is the contract its own header documents. Without the unit installed we
# start it ourselves below.
log "creating OpenGrok container ($IMAGE) on port $PORT"
podman create --name "$CONTAINER" \
  -p "$PORT:8080" \
  --security-opt label=disable \
  -e NOMIRROR=1 \
  -e SYNC_PERIOD_MINUTES=0 \
  -e INDEXER_JAVA_OPTS="$INDEXER_JAVA_OPTS" \
  -e CATALINA_OPTS="${CATALINA_OPTS:--Xmx24g}" \
  -e WORKERS="$WORKERS" \
  -e INDEXER_OPT="--disableRepository Perforce --disableRepository git --disableRepository CVS --canonicalRoot $SRV_ROOT/$IGNORE_OPT" \
  -v "$HERE/entrypoint-ro.sh:/scripts/entrypoint-ro.sh:ro" \
  "${mounts[@]}" \
  -v "$DATA_VOLUME:/opengrok/data" \
  -v "$ETC_VOLUME:/opengrok/etc" \
  --entrypoint /scripts/entrypoint-ro.sh \
  "$IMAGE"

if unit_installed; then
  log "starting via $OPENGROK_UNIT (it owns the running container)"
  systemctl --user start "$OPENGROK_UNIT"
else
  log "starting container directly ($OPENGROK_UNIT not installed)"
  podman start "$CONTAINER" >/dev/null
fi

# 3. disable the suggester's nightly rebuild. It defaults to a midnight
#    (container-local, i.e. UTC) cron that burns ~10-15min of full CPU
#    rebuilding autocomplete indexes for every project -- a feature nobody
#    here uses interactively (search happens via casket-mcp/REST/grep, not
#    the web UI's suggestion dropdown). This is applied on every container
#    (re)creation; even though /opengrok/etc is now persisted (ETC_VOLUME),
#    the persisted configuration.xml doesn't reliably carry the disabled
#    flag, and the PUT is cheap and idempotent, so we just re-apply it here.
# 2b. verify the host-side port forward actually came up. Rootless podman on
#    pasta sometimes creates the container with the port binding declared but
#    no forwarder listening on the host: Tomcat answers fine INSIDE the
#    container while curl from the host gets connection-refused, which reads
#    exactly like a wedged webapp and sent us chasing a GC death spiral for
#    hours on 2026-07-28. Same class as the pasta default-route race fixed in
#    f3af1ca. A plain restart re-runs the network setup and fixes it.
#
#    The check below only proves the forwarder came up, not that it STAYS up:
#    on 2026-08-02 it logged "host port :8080 is listening" and the port was
#    gone ~15 minutes later, because the opengrok.service restart loop was
#    still churning the container's network. That loop is fixed above, so the
#    re-check at the end of this script (2c) is the guard against it coming
#    back by another route.
log "verifying host-side port forward on :$PORT"
port_up() { (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ":${PORT}[[:space:]]"; }
for attempt in 1 2; do
  for _ in $(seq 1 20); do
    port_up && break
    sleep 1
  done
  port_up && break
  if [ "$attempt" = 1 ]; then
    log "WARNING: nothing listening on host :$PORT (pasta forwarder missing) — restarting container"
    podman restart "$CONTAINER" >/dev/null 2>&1 || true
    sleep 5
  fi
done
if port_up; then
  log "host port :$PORT is listening"
else
  log "WARNING: host :$PORT still not listening. The webapp may be reachable"
  log "         only inside the container — check with:"
  log "           podman exec $CONTAINER curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/v1/system/ping"
  log "         and recreate the container if it returns 200."
fi

log "waiting for webapp API and disabling suggester rebuild..."
token=""
for _ in $(seq 1 60); do
  token="$(podman exec "$CONTAINER" cat /opengrok/etc/webapp_api_token 2>/dev/null || true)"
  [ -n "$token" ] && curl -sf -o /dev/null "http://localhost:$PORT/api/v1/configuration" -H "Authorization: Bearer $token" && break
  sleep 1
done
if [ -n "${token:-}" ]; then
  cfg="$(curl -s -H "Authorization: Bearer $token" "http://localhost:$PORT/api/v1/configuration")"
  patched="$(printf '%s' "$cfg" | sed 's#<void id="SuggesterConfig0" property="suggesterConfig">#<void id="SuggesterConfig0" property="suggesterConfig">\n   <void property="enabled"><boolean>false</boolean></void>#')"
  if curl -sf -o /dev/null -X PUT -H "Authorization: Bearer $token" -H "Content-Type: application/xml" --data-binary "$patched" "http://localhost:$PORT/api/v1/configuration"; then
    log "suggester rebuild disabled"
  else
    log "WARNING: failed to disable suggester rebuild (PUT failed) -- check manually"
  fi
else
  log "WARNING: webapp API did not come up in time -- suggester rebuild NOT disabled, check manually"
fi

# 2c. re-check the forwarder now that the container has been up for a while and
#     the API work above has exercised it. The startup check (2b) fires seconds
#     after `podman start`, which is too early to catch a forwarder that dies
#     under later churn -- the exact 2026-08-02 failure. Costs nothing on a
#     healthy run.
if port_up; then
  log "host port :$PORT still listening after startup"
else
  log "WARNING: host :$PORT came up but is NOT listening any more — the pasta"
  log "         forwarder died after startup. Tomcat is probably fine inside"
  log "         the container; check for something else managing this container"
  log "         (systemctl --user status $OPENGROK_UNIT — a restart loop there"
  log "         does this) and recover with: podman restart $CONTAINER"
fi

# Best-effort: report the LAN URL.
ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
log "started. Indexing runs in the background (podman logs -f $CONTAINER)."
log "Web UI:   http://${ip:-<host>}:$PORT/"
log "          http://localhost:$PORT/   (on this host)"
log ""
log "If accessing from the LAN, open the firewall port:"
log "    sudo firewall-cmd --add-port=$PORT/tcp            # until reboot"
log "    sudo firewall-cmd --add-port=$PORT/tcp --permanent && sudo firewall-cmd --reload"

