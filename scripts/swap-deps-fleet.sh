#!/usr/bin/env bash
# Point every mount at its deps-enabled rebuild (casket-<DATE>-*), for the whole
# fleet at once.
#
#   registry-tracked (a / b / b-operand): add a new entry carrying the SAME
#     fingerprint as the live one (the content is identical -- the repack only
#     adds deps/), promote it to live, retire the old entry.
#   static-mounts.tsv (certified / community): rewrite the artifact path.
#
# Phase A keeps every patch mount live (additive semantics), so the old entry is
# retired by ENTRY ID, never with --retire-previous -- that would retire the
# other patches of the same minor too.
#
# Usage: swap-deps-fleet.sh --date YYYYMMDD [--apply]
# Dry-run by default. --apply only rewrites registry/config; run
# `sudo casket-mounts.sh --apply` afterwards to move the actual mounts.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

DATE=""; APPLY=0; OUT_DIR="${CASKET_OUT}"
while (( $# )); do
    case "$1" in
        --date)  DATE="$2"; shift 2 ;;
        --outdir) OUT_DIR="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) echo "swap-deps-fleet.sh --date YYYYMMDD [--apply]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$DATE" ]] || die "--date required"

STATIC="$CONFIG_DIR/static-mounts.tsv"

# ---- 1. registry-tracked mounts
DATE="$DATE" OUT_DIR="$OUT_DIR" APPLY="$APPLY" REGISTRY_PY="$REGISTRY_PY" \
CASKET_WORK="$CASKET_WORK" python3 - <<'PY'
import json, os, subprocess, sys
date, out_dir = os.environ["DATE"], os.environ["OUT_DIR"].rstrip("/")
apply_ = os.environ["APPLY"] == "1"
reg_py = os.environ["REGISTRY_PY"]
reg = json.load(open(f"{os.environ['CASKET_WORK']}/state/registry.json"))
entries = reg["artifacts"] if isinstance(reg, dict) else reg

def new_artifact(mount_path):
    # /srv/sources-<suffix> -> <out>/casket-<date>-<suffix>.sqfs.xz
    suffix = mount_path.rsplit("/", 1)[-1].replace("sources-", "", 1)
    return f"{out_dir}/casket-{date}-{suffix}.sqfs.xz"

todo, skipped = [], []
for e in entries:
    if e.get("status") != "live":
        continue
    if e.get("phase") == "a-rpm":
        skipped.append((e, "a-rpm: SRPM casket has no lockfiles, not repacked"))
        continue
    art = new_artifact(e["mount_path"])
    if not os.path.exists(art):
        skipped.append((e, f"no rebuild at {art}"))
        continue
    todo.append((e, art))

for e, why in skipped:
    print(f"  - skip {e['phase']}-{e['unit']} {e['mount_path']}: {why}")

for e, art in todo:
    print(f"  + {e['phase']}-{e['unit']} {e['mount_path']}")
    print(f"      {os.path.basename(e['artifact_path'])} -> {os.path.basename(art)}")
    if not apply_:
        continue
    new_id = subprocess.run(
        ["python3", reg_py, "add", "--phase", e["phase"], "--unit", str(e["unit"]),
         "--fingerprint", e["fingerprint"], "--artifact-path", art,
         "--mount-path", e["mount_path"]],
        capture_output=True, text=True, check=True).stdout.strip()
    # promote WITHOUT --retire-previous: phase a keeps its other patch mounts live
    subprocess.run(["python3", reg_py, "transition", "--entry-id", new_id,
                    "--to", "live"], check=True, capture_output=True)
    subprocess.run(["python3", reg_py, "transition", "--entry-id", str(e["entry_id"]),
                    "--to", "retired"], check=True, capture_output=True)
    print(f"      entry {e['entry_id']} retired, {new_id} live")
print(f"registry: {len(todo)} to swap, {len(skipped)} skipped")
PY
rc=$?
(( rc == 0 )) || die "registry step failed (rc=$rc)"

# ---- 2. static mounts (certified / community): rewrite the artifact column
n=0
while IFS=$'\t' read -r artifact mount; do
    [[ -z "$artifact" || "$artifact" == \#* ]] && continue
    case "$mount" in *-certified-operators|*-community-operators) ;; *) continue ;; esac
    suffix="${mount##*/}"; suffix="${suffix/sources-/}"
    new="${OUT_DIR%/}/casket-${DATE}-${suffix}.sqfs.xz"
    [[ -s "$new" ]] || { log "  - skip $mount (no rebuild)"; continue; }
    [[ "$artifact" == "$new" ]] && continue
    echo "  + $mount"
    echo "      $(basename "$artifact") -> $(basename "$new")"
    if (( APPLY )); then
        python3 - "$STATIC" "$mount" "$new" <<'PY'
import sys
path, mount, new = sys.argv[1:4]
lines = open(path).read().splitlines(keepends=True)
with open(path, "w") as f:
    for line in lines:
        if line.startswith("#") or "\t" not in line:
            f.write(line); continue
        art, mnt = line.rstrip("\n").split("\t", 1)
        f.write(f"{new}\t{mnt}\n" if mnt == mount else line)
PY
    fi
    n=$((n+1))
done < "$STATIC"
log "static-mounts: $n line(s) $( (( APPLY )) && echo rewritten || echo "would be rewritten")"

(( APPLY )) || echo "(dry-run; re-run with --apply, then: sudo scripts/casket-mounts.sh --apply)"
