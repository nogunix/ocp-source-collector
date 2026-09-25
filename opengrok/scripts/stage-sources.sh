#!/usr/bin/env bash
#
# stage-sources.sh — build a clean symlink tree for OpenGrok out of the
# casket-ocp squashfs mounts under /srv/sources-*.
#
# OpenGrok turns each first-level directory under its source root into a
# "project". The raw casket layout (/srv/sources-ocp<ver>/git/<name>-<sha>/)
# is noisy: the intermediate `git/` and the trailing commit hash make ugly
# project/file paths. This script produces:
#
#   /srv/opengrok-src/
#   ├── ocp-<ver>/<clean-name>      -> /srv/sources-ocp<ver>/git/<name>-<sha>      (Phase A)
#   ├── operators-<minor>/<clean>   -> /srv/sources-ocp<minor>-operators/git/...   (Phase B)
#   ├── certified-<minor>/<clean>   -> /srv/sources-ocp<minor>-certified-.../git/  (Phase B-certified)
#   ├── community-<minor>/<clean>   -> /srv/sources-ocp<minor>-community-.../git/  (Phase B-community)
#   ├── layered-<minor>/<product>/  -> Phase B-operand per-product git trees
#   └── srpms-<ver>/<pkg>           -> /srv/sources-ocp-srpms/srpms/<NEVR>        (Phase A-rpm, one project per OCP version)
#
# Only `git/` (the components' own source) is staged. Each casket also carries
# `deps/` -- the ~700k language-level dependency sources collected 2026-07-26 --
# and that is DELIBERATELY left out of the index: it dwarfs the sources it
# belongs to, and the index already takes ~10h to rebuild without it (decision
# 2026-07-29). Search dependency source with ripgrep over the mount, or via
# casket-mcp, e.g.
#     rg -n "fn read_tls" /srv/sources-layered-ocp4.22/trustee/deps/crates/rustls-0.23.40/
# `meta/DEPS.tsv` in each casket maps component -> ecosystem/name/version/dir.
#
# A component whose casket carries no source at all gets a single
# SOURCE-NOT-COLLECTED.txt instead of an empty directory: that is routine for
# Phase B-operand (97 of 146 layered-4.18 products as of 2026-07-29) and an
# empty project reads to a user as a broken index, since /xref/<proj>/<name>/
# 404s. The note names the reason per component and, where the payload happens
# to carry the same repo, the ocp-<patch> project that has the exact commit.
#
# Only minors listed in config/opengrok-minors.txt are staged. Unlisted
# minors remain searchable via ripgrep through casket-mcp.
#
# Within an allowed minor, only the newest `keep-patches:` z-stream releases
# are staged (default 1). Phase A's swap is deliberately additive -- a patch
# bump is a new mountpoint and the old patch stays live forever, because
# browsing an exact past patch offline is the point of the caskets
# (casket-swap.sh "Mount naming"). Nothing retires those, so without a cap the
# project list grows without bound, and an ocp-<patch> project costs ~33G of
# SSD (19G index + 14G xref, measured 2026-07-29) against ~2.6G of HDD for the
# casket itself. The cap bounds the *index* only: every patch stays mounted
# under /srv/sources-ocp<patch> and stays reachable via ripgrep/casket-mcp.
# Applies to the patch-scoped phases (A: ocp-<ver>, A-rpm: srpms-<ver>);
# B/B-operand are minor-scoped and unaffected.
#
# Dropping a patch out of the cap leaves its index/xref behind under
# /srv/opengrok-data. Pass --prune-index to delete those orphans; without it
# they are only reported.
#
# The symlink targets are absolute /srv paths, so the OpenGrok container must
# bind-mount both this staging tree AND the underlying /srv/sources-* mounts at
# their real paths, and be told `--canonicalRoot /srv/` so it follows the links.
# run-opengrok.sh does that automatically.
#
# Idempotent: rebuilds the whole tree from scratch each run. Needs root to
# write under /srv (re-execs itself via sudo).
#
set -euo pipefail

PRUNE_INDEX=0
for arg in "$@"; do
  case "$arg" in
    --prune-index) PRUNE_INDEX=1 ;;
    -h|--help) echo "stage-sources.sh [--prune-index]"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# Writing under /srv requires root; re-exec via sudo if needed.
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

STAGE_DIR="${STAGE_DIR:-/srv/opengrok-src}"
SRV_ROOT="${SRV_ROOT:-/srv}"
DATA_DIR="${DATA_DIR:-/srv/opengrok-data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${CONFIG_DIR:-$SCRIPT_DIR/../../config}"
# shellcheck source=lib-stage.sh
source "$SCRIPT_DIR/lib-stage.sh"   # cap_patches, is_kept, payload_only, link_git_phase

log() { printf '[stage-sources] %s\n' "$*" >&2; }

# Load the OpenGrok minor whitelist. Fall back to all minors if the
# opengrok-specific file doesn't exist.
OPENGROK_MINORS_FILE="$CONFIG_DIR/opengrok-minors.txt"
if [ ! -f "$OPENGROK_MINORS_FILE" ]; then
  OPENGROK_MINORS_FILE="$CONFIG_DIR/minors.txt"
  log "WARNING: config/opengrok-minors.txt not found, falling back to minors.txt (all minors)"
fi
# Read into an associative array for O(1) lookup. A `keep-patches: N` line
# sets the per-minor z-stream cap; anything else is a minor.
declare -A ALLOWED_MINORS
KEEP_PATCHES_CFG=1
while IFS= read -r line; do
  line="${line%%#*}"         # strip comments
  line="${line// /}"         # strip whitespace
  [ -z "$line" ] && continue
  case "$line" in
    keep-patches:*) KEEP_PATCHES_CFG="${line#keep-patches:}"; continue ;;
  esac
  ALLOWED_MINORS["$line"]=1
done < "$OPENGROK_MINORS_FILE"
# Env wins over the config file (for one-off full re-stages).
KEEP_PATCHES="${KEEP_PATCHES:-$KEEP_PATCHES_CFG}"
case "$KEEP_PATCHES" in
  ''|*[!0-9]*) log "ERROR: keep-patches must be a non-negative integer, got '$KEEP_PATCHES'"; exit 2 ;;
esac
log "OpenGrok minors: ${!ALLOWED_MINORS[*]}"
if [ "$KEEP_PATCHES" -eq 0 ]; then
  log "keep-patches: 0 (unlimited — every mounted patch is staged)"
else
  log "keep-patches: $KEEP_PATCHES newest z-stream per minor (older patches stay mounted, ripgrep-only)"
fi

# Check whether a minor (e.g. "4.20") or patch version (e.g. "4.20.27")
# is in the whitelist.
is_allowed() {
  local ver="$1"
  # exact match (minor)
  [ "${ALLOWED_MINORS[$ver]+_}" ] && return 0
  # patch version -> strip last dot segment to get minor
  local minor="${ver%.*}"
  [ "${ALLOWED_MINORS[$minor]+_}" ] && return 0
  return 1
}

# strip_sha / link_git_phase live in lib-stage.sh (tests/test_stage.sh).
#
# link_git_phase returns 1 when the casket carried no source for that component.
# Rather than leave the path 404ing (which reads as an indexing bug -- it was
# reported as one on 2026-07-30 for layered-4.18/service-registry-operator), the
# project gets a single explanatory text file naming, per component, why there
# is no tree and where the source can be found instead. That costs ~1KB of index
# against ~20G/minor for the alternative of symlinking the Phase A copies in
# (1.6G of shared infra source x ~13 index+xref multiplier, measured 4.18).
EMPTY=""
PHASE_A_MAP="$(mktemp)"        # minor TAB repo TAB ocp-<ver>/<dir>
trap 'rm -f "$PHASE_A_MAP"' EXIT

# stage_or_note_empty <gitdir> <projdir> [minor]
stage_or_note_empty() {
  local gitdir="$1" proj="$2" minor="${3:-}"
  if ! link_git_phase "$gitdir" "$proj"; then
    EMPTY="$EMPTY${proj#$STAGE_DIR/}
"
    write_source_note "$(dirname "$gitdir")" "$proj/SOURCE-NOT-COLLECTED.txt" \
      "$minor" "$(basename "$proj")"
  fi
}

# write_source_note <casket-component-dir> <note-file> <minor> <name>
# <casket-component-dir> is the dir holding git/ and meta/ on the mount.
write_source_note() {
  local casket="$1" note="$2" minor="${3:-}" name="$4"
  mkdir -p "$(dirname "$note")"
  {
    printf 'No source was collected for %s.\n\n' "$name"
    printf 'This is a collection gap, not an indexing gap: the casket at\n'
    printf '  %s\n' "$casket"
    printf 'has an empty git/, so OpenGrok has nothing to index here. Per-component\n'
    printf 'reasons below, from that casket'"'"'s meta/git.tsv.\n\n'
    if [ -r "$casket/meta/git.tsv" ]; then
      awk -F'\t' -v mapf="$PHASE_A_MAP" -v minor="$minor" '
        BEGIN {
          while ((getline line < mapf) > 0) {
            split(line, f, "\t")
            if (minor != "" && f[1] == minor) where[f[2]] = f[3]
          }
        }
        {
          comp = $1; src = $2; ref = $3; ver = $5; method = $6
          if (src == "") {
            printf "  %-46s no source label on the image (method %s)\n", comp, method
            next
          }
          printf "  %-46s %s\n", comp, src
          printf "  %-46s   labelled ref %s (version %s) is not on public github\n", "", (ref == "" ? "-" : ref), (ver == "" ? "-" : ver)
          if (src in where)
            printf "  %-46s   the payload build of this repo IS indexed: %s\n", "", where[src]
        }' "$casket/meta/git.tsv"
    else
      printf '  (no meta/git.tsv in the casket)\n'
    fi
    printf '\nWhere to look instead:\n'
    printf '  - a "payload build IS indexed" line above points at the exact commit\n'
    printf '    OpenGrok already has, in the Phase A project for this release.\n'
    printf '  - dependency source is never indexed; ripgrep the mount directly, e.g.\n'
    printf '      rg -n "<pattern>" %s/deps/\n' "$casket"
    printf '  - meta/git.tsv and meta/labels.tsv on the mount are the raw record.\n'
  } > "$note"
}

# write_sidecar_readme <layered-mount> <readme> <minor> <newline-separated products>
write_sidecar_readme() {
  local mount="$1" readme="$2" minor="$3" prods="$4" prod
  {
    printf 'Products whose collected source is ONLY sidecar images (kube-rbac-proxy,\n'
    printf 'oauth-proxy, oc, CSI sidecars, ...) that the %s payload also ships.\n\n' "$minor"
    printf 'The product'"'"'s own operator/operand resolved to no source, so these trees\n'
    printf 'are not the product -- they are grouped here instead of at the top of\n'
    printf 'layered-%s/. They are still the exact builds each product ships, which\n' "$minor"
    printf 'differ from the payload commit, so they stay indexed.\n\n'
    printf 'Per product: repo, the commit it ships, and the payload build of that repo.\n\n'
    while IFS= read -r prod; do
      [ -n "$prod" ] || continue
      printf '%s\n' "$prod"
      awk -F'\t' -v mapf="$PHASE_A_MAP" -v minor="$minor" '
        BEGIN {
          while ((getline line < mapf) > 0) {
            split(line, f, "\t")
            if (f[1] == minor) where[f[2]] = f[3]
          }
        }
        FNR > 1 && $1 != "" {
          printf "  %-46s %.12s  (payload: %s)\n", $2, $3, where[$2]
        }' "$mount/$prod/git/INDEX.tsv"
      printf '\n'
    done <<< "$prods"
  } > "$readme"
}

log "rebuilding staging tree at $STAGE_DIR"
# Clear the CONTENTS, never the directory itself. $STAGE_DIR is bind-mounted
# into the running OpenGrok container, and a bind mount resolves to an inode:
# `rm -rf $STAGE_DIR && mkdir` makes a NEW inode, leaving the container looking
# at the deleted one -- /opengrok/src reads as empty inside, every project
# vanishes from the webapp, and every /xref/<anything> 404s until the container
# is recreated. Hit that on 2026-07-30 by re-staging under a live container.
mkdir -p "$STAGE_DIR"
find "$STAGE_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +

# Phase A: /srv/sources-ocp<ver>/  (ver like 4.20.22 — three dotted parts; the
# *.*.* glob excludes the single-dotted "<minor>-operators" dirs and srpms).
a_all=""
for m in "$SRV_ROOT"/sources-ocp[0-9]*.[0-9]*.[0-9]*; do
  [ -d "$m/git" ] || continue
  case "$m" in *-operators) continue ;; esac
  ver="$(basename "$m" | sed -E 's/^sources-ocp//')"
  is_allowed "$ver" || { log "Phase A: ocp-$ver (skipped — minor not in whitelist)"; continue; }
  a_all="$a_all$ver
"
done
a_keep="$(printf '%s' "$a_all" | cap_patches "$KEEP_PATCHES")"
for ver in $(printf '%s' "$a_all" | sort -V); do
  if ! is_kept "$ver" "$a_keep"; then
    log "Phase A: ocp-$ver (skipped — not among the newest $KEEP_PATCHES patches of $(track_of "$ver"); still mounted at $SRV_ROOT/sources-ocp$ver)"
    continue
  fi
  log "Phase A: ocp-$ver"
  stage_or_note_empty "$SRV_ROOT/sources-ocp$ver/git" "$STAGE_DIR/ocp-$ver" "${ver%.*}"
  # repo -> staged project path, so a layered product that could not fetch a
  # repo can at least point at the payload build of it. Only 17 of the 82
  # unfetchable layered-4.18 repos are in the payload (measured 2026-07-30) --
  # metallb, sriov-*, velero, ViaQ/*, migtools/* are layered-only and are NOT
  # covered by Phase A, so this is a partial pointer, never a substitute.
  # Keyed by minor: a layered-4.18 product must be pointed at ocp-4.18.48, never
  # at another minor's payload.
  idx="$SRV_ROOT/sources-ocp$ver/git/INDEX.tsv"
  [ -r "$idx" ] && awk -F'\t' -v p="ocp-$ver" -v m="${ver%.*}" 'FNR>1 && $2!="" {
      d=$1; sub(/-[0-9a-f]{7,40}$/, "", d); print m"\t"$2"\t"p"/"d
  }' "$idx" >> "$PHASE_A_MAP"
done
sort -u -o "$PHASE_A_MAP" "$PHASE_A_MAP"

# Phase B: /srv/sources-ocp<minor>-operators/  (minor like 4.20).
# The certified/community catalog mounts also match this glob — route them
# to their own catalog-<minor> project names instead.
for m in "$SRV_ROOT"/sources-ocp[0-9]*-operators; do
  [ -d "$m/git" ] || continue
  base="$(basename "$m" | sed -E 's/^sources-ocp//; s/-operators$//')"
  case "$base" in
    *-certified)  minor="${base%-certified}";  proj="certified-$minor" ;;
    *-community)  minor="${base%-community}";  proj="community-$minor" ;;
    *)            minor="$base";               proj="operators-$minor" ;;
  esac
  is_allowed "$minor" || { log "Phase B: $proj (skipped)"; continue; }
  log "Phase B: $proj"
  stage_or_note_empty "$m/git" "$STAGE_DIR/$proj" "$minor"
done

# Phase B-operand: /srv/sources-layered-ocp<minor>/<product>/git/  (cnv/acs/mce/...)
#   -> layered-<minor>/<product>/<clean-name>  (one project per minor, product subdirs)
#
# Only products with source of their own sit at the top of layered-<minor>/.
# Two kinds are grouped out of the way, because at the top they made the
# listing unreadable (4.20: 29 stubs of ~16 lines and 25 sidecar-only products
# among 149, reported 2026-09-25):
#   _not-collected/<product>.txt  no tree at all (the SOURCE-NOT-COLLECTED note)
#   _sidecar-only/<product>/      only payload-repo sidecar builds (payload_only);
#                                 still symlinked and indexed, since none is the
#                                 payload commit
for m in "$SRV_ROOT"/sources-layered-ocp[0-9]*; do
  [ -d "$m" ] || continue
  minor="$(basename "$m" | sed -E 's/^sources-layered-ocp//')"
  is_allowed "$minor" || { log "Phase B-operand: layered-$minor (skipped)"; continue; }
  lay="$STAGE_DIR/layered-$minor"
  sidecar=""
  for p in "$m"/*/; do
    [ -d "$p/git" ] || continue
    prod="$(basename "$p")"
    if payload_only "$p/git/INDEX.tsv" "$PHASE_A_MAP" "$minor"; then
      log "Phase B-operand: layered-$minor/_sidecar-only/$prod (payload-repo sidecars only)"
      link_git_phase "$p/git" "$lay/_sidecar-only/$prod" || true
      sidecar="$sidecar$prod
"
    else
      log "Phase B-operand: layered-$minor/$prod"
      if ! link_git_phase "$p/git" "$lay/$prod"; then
        EMPTY="${EMPTY}layered-$minor/_not-collected/$prod
"
        write_source_note "${p%/}" "$lay/_not-collected/$prod.txt" "$minor" "$prod"
      fi
    fi
  done
  if [ -n "$sidecar" ]; then
    write_sidecar_readme "$m" "$lay/_sidecar-only/README.txt" "$minor" "$sidecar"
  fi
done

# Phase A-rpm: stage SRPMs as ONE PROJECT PER OCP VERSION (srpms-<ver>), to
# mirror the Phase A/B git layout (ocp-<ver>, operators-<minor>). Each project
# is exactly that release's SRPM set, taken from by-ocp/<ver>/<name> ->
# srpms/<NEVR>. Unlike git sources, SRPMs are heavily shared across minors, so
# a shared NEVR gets indexed once per version project it appears in (~1.55x the
# unique count for the 3 whitelisted minors) — the cost of the per-version view.
#
# Within a single version the by-ocp entry names are already clean and unique
# (bare "kernel"/"samba", with an NEVR suffix only on the rare (V,R)-collision
# second package), so we use them verbatim — no cross-version dedup, hence none
# of the "first minor wins, later versions dropped" bug the old single-project
# staging had (bare "samba" pinned to 4.18's 4.19.4, hiding 4.20/4.22's 4.23.5).
srpms_dir="$SRV_ROOT/sources-ocp-srpms"
if [ -d "$srpms_dir/by-ocp" ]; then
  s_all=""
  for ocp_ver in "$srpms_dir"/by-ocp/*/; do
    [ -d "$ocp_ver" ] || continue
    ver="$(basename "$ocp_ver")"
    is_allowed "$ver" || continue
    s_all="$s_all$ver
"
  done
  s_keep="$(printf '%s' "$s_all" | cap_patches "$KEEP_PATCHES")"
  for ver in $(printf '%s' "$s_all" | sort -V); do
    if ! is_kept "$ver" "$s_keep"; then
      log "Phase A-rpm: srpms-$ver (skipped — not among the newest $KEEP_PATCHES patches of $(track_of "$ver"))"
      continue
    fi
    ocp_ver="$srpms_dir/by-ocp/$ver/"
    proj="$STAGE_DIR/srpms-$ver"
    mkdir -p "$proj"
    n=0
    for pkg in "$ocp_ver"*/; do
      [ -L "$pkg" ] || [ -d "$pkg" ] || continue
      real="$(readlink -f "$pkg")"
      [ -d "$real" ] || continue
      name="$(basename "$pkg")"   # by-ocp clean per-release name (kernel/samba/...)
      ln -sfn "$real" "$proj/$name"
      n=$((n + 1))
    done
    log "Phase A-rpm: srpms-$ver ($n SRPMs)"
  done
else
  log "Phase A-rpm: srpms (skipped — by-ocp/ not found)"
fi

log "done. projects:"
ls -1 "$STAGE_DIR" >&2

# --- symlink cycles ------------------------------------------------------
# A cycle anywhere under the staged tree makes OpenGrok's post-index cleanup
# (PendingFileCompleter.findFilelessChildren) recurse without bound: no cycle
# check, no depth limit, no log line -- one core at 100% forever, which reads
# as "still indexing". srpms-4.22.4 burned 2h50m of CPU that way on
# 2026-08-08 before it was killed by hand; the culprit was fwupd-2.0.19's mock
# sysfs fixture. Cheap to detect here, expensive to diagnose later.
#
# Reported, not fatal: the fix is to add the subtree to
# config/opengrok-ignore.txt (which run-opengrok.sh feeds to the indexer as
# -i), and that is a judgement call about which source stops being browsable.
# A cycle already covered by the ignore list still shows up here, because this
# scan does not parse the patterns -- so cross-check before acting.
if [ "${SKIP_CYCLE_CHECK:-0}" = "1" ]; then
  log "symlink cycle check skipped (SKIP_CYCLE_CHECK=1)"
elif ! command -v python3 >/dev/null 2>&1; then
  log "WARNING: python3 not found — symlink cycle check skipped"
else
  log "scanning the staged tree for symlink cycles (walks every indexed file; minutes)"
  cycles="$("$SCRIPT_DIR/find-symlink-cycles.py" -q "$STAGE_DIR" || true)"
  if [ -n "$cycles" ]; then
    log "SYMLINK CYCLES FOUND ($(printf '%s\n' "$cycles" | grep -c .)) — the indexer will"
    log "  spin forever on these unless the subtree is in config/opengrok-ignore.txt:"
    printf '%s\n' "$cycles" | sed 's/^/[stage-sources]   /' >&2
  else
    log "no symlink cycles under $STAGE_DIR"
  fi
fi

if [ -n "$EMPTY" ]; then
  log "no source collected — SOURCE-NOT-COLLECTED.txt written instead ($(printf '%s' "$EMPTY" | grep -c . ) entries):"
  printf '%s' "$EMPTY" | sed 's/^/[stage-sources]   /' >&2
fi

# --- orphaned index/xref -------------------------------------------------
# A project that dropped out of the whitelist or the keep-patches cap leaves
# its index and xref behind under $DATA_DIR (~33G for an ocp-<patch>). The
# webapp forgets the project once it is gone from the source root, so these
# are dead weight, but deleting index data is not this script's job by
# default -- report them and let --prune-index do it explicitly.
orphans=""
for sub in index xref; do
  [ -d "$DATA_DIR/$sub" ] || continue
  for d in "$DATA_DIR/$sub"/*/; do
    [ -d "$d" ] || continue
    p="$(basename "$d")"
    [ -e "$STAGE_DIR/$p" ] && continue
    orphans="$orphans$DATA_DIR/$sub/$p
"
  done
done

if [ -z "$orphans" ]; then
  log "no orphaned index/xref under $DATA_DIR"
elif [ "$PRUNE_INDEX" -eq 1 ]; then
  printf '%s' "$orphans" | while IFS= read -r d; do
    [ -n "$d" ] || continue
    log "pruning $d ($(du -sh "$d" 2>/dev/null | cut -f1))"
    rm -rf -- "$d"
  done
  log "orphaned index/xref pruned"
else
  log "orphaned index/xref (no longer staged) — re-run with --prune-index to delete:"
  printf '%s' "$orphans" | while IFS= read -r d; do
    [ -n "$d" ] || continue
    log "  $d ($(du -sh "$d" 2>/dev/null | cut -f1))"
  done
fi

