#!/bin/bash
# Build a by-OCP-version symlink tree inside the Phase A/RPM stage so that,
# on the mounted casket, you can tell which OCP minor each SRPM belongs to
# without reading meta/rpmdb/*.tsv. SRPMs stay deduped in srpms/<NEVR>/; this
# only adds cheap symlinks:
#
#   by-ocp/<ocp-version>/<src-name> -> ../../srpms/<NEVR>
#
# Mapping tsv (binary RPM) -> srpms/<NEVR> (source RPM) is done by:
#   1. (V,R) match: an installed binary's version-release equals its source's
#      (NEVR version/release carry no '-', so we split the dir name from the
#      right). Resolves ~99% with zero spec parsing.
#   2. rpmspec disambiguation when one (V,R) maps to several sources: query each
#      spec (with _sourcedir=archives) for the binary names it produces.
#   3. prefix fallback (incl. stripping pythonN-/lib subpackage prefixes).
#
# Also writes meta/bin-to-src.tsv (binary-NEVRA <TAB> source-NEVR) as an index.
#
# Usage: phase-a-rpm-by-ocp.sh [-s STAGE] [-r RPMDB_DIR] [-j JOBS]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

STAGE="${CASKET_WORK}/phase-a-rpm/50-out/stage"
RPMDB_DIR="${CASKET_WORK}/phase-a-rpm/rpmdb"
JOBS=8
while (( $# )); do
  case "$1" in
    -s|--stage) STAGE="$2"; shift 2 ;;
    -r|--rpmdb) RPMDB_DIR="$2"; shift 2 ;;
    -j|--jobs)  JOBS="$2"; shift 2 ;;
    -h|--help)  echo "phase-a-rpm-by-ocp.sh [-s STAGE] [-r RPMDB_DIR] [-j JOBS]"; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done
require_cmd rpmspec python3 xargs

SRPMS="$STAGE/srpms"
[[ -d "$SRPMS"     ]] || die "no extracted SRPMs at $SRPMS"
[[ -d "$RPMDB_DIR" ]] || die "no rpmdb tsvs at $RPMDB_DIR"

# 1. Best-effort binary-name -> source-NEVR map via rpmspec. Many el9 specs fail
#    to parse under a newer host rpm (old %patchN syntax); that's fine, (V,R)
#    matching covers them. We only need this to disambiguate (V,R) collisions.
MAP=$(mktemp)
# Scratch cwd for the rpmspec queries below. Parsing a spec is not read-only:
# %{lua:} runs at PARSE time, and io.open() with a relative path writes into
# whatever cwd rpmspec inherited. google-noto-fonts does exactly this
# (debug-noto-fcconf-build.sh, debug-noto-metainfo-build.sh), so on 2026-08-11
# a refresh run dropped two root-owned files straight into the repo checkout --
# by-ocp.sh had simply inherited the caller's cwd, and it runs under sudo.
# Nothing was missing from the casket; the files were never SRPM payload.
# Each worker gets its own subdir so parallel specs writing the same filename
# cannot race, and the whole tree is thrown away on exit.
SCRATCH=$(mktemp -d)
trap 'rm -f "$MAP"; rm -rf "$SCRATCH"' EXIT
log "building rpmspec bin->src map (-P $JOBS, failures expected & harmless)"
( cd "$SRPMS" && ls -d */ | sed 's#/##' ) | \
  MAP="$MAP" SRPMS="$SRPMS" SCRATCH="$SCRATCH" xargs -P "$JOBS" -I{} bash -c '
    d="{}"; spec=$(ls "$SRPMS/$d"/info/*.spec 2>/dev/null | head -1); [[ -n "$spec" ]] || exit 0
    work="$SCRATCH/$d"; mkdir -p "$work" || exit 0
    names=$(cd "$work" && rpmspec -q --define "_sourcedir $SRPMS/$d/archives" --qf "%{NAME}\n" "$spec" 2>/dev/null) || exit 0
    while read -r b; do [[ -n "$b" ]] && printf "%s\t%s\n" "$b" "$d"; done <<< "$names" >> "$MAP"
  '
log "rpmspec mapped $(wc -l < "$MAP") binary names"

# 2. Resolve tsvs -> symlink tree + bin-to-src.tsv.
log "resolving per-version sources and building by-ocp/ symlink tree"
MAP="$MAP" SRPMS="$SRPMS" RPMDB_DIR="$RPMDB_DIR" STAGE="$STAGE" python3 - <<'PY'
import os, glob, collections
stage=os.environ["STAGE"]; srpms=os.environ["SRPMS"]
rpmdb=os.environ["RPMDB_DIR"]; mapf=os.environ["MAP"]

dirs=[d for d in os.listdir(srpms) if os.path.isdir(os.path.join(srpms,d))]
def split_nvr(n):
    a=n.rsplit('-',2)
    return tuple(a) if len(a)==3 else None
vr2src=collections.defaultdict(list); src_name={}
for d in dirs:
    p=split_nvr(d)
    if p:
        vr2src[(p[1],p[2])].append(d); src_name[d]=p[0]

import re, functools
name2rels=collections.defaultdict(list)   # source NAME -> [source dir, ...]
for d in dirs:
    p=split_nvr(d)
    if p: name2rels[p[0]].append(d)

bin2src={}                                 # binary NAME -> a source dir (rpmspec)
bin2name={}                                # binary NAME -> source NAME
for line in open(mapf):
    line=line.rstrip("\n")
    if "\t" in line:
        b,s=line.split("\t",1); s=s.rstrip("/")
        bin2src[b]=s
        if s in src_name: bin2name[b]=src_name[s]

# --- rpm-style release comparison, for nearest-release selection ---
_seg=re.compile(r'\d+|[A-Za-z]+')
def _rpmvercmp(a,b):
    ta=_seg.findall(a); tb=_seg.findall(b)
    for x,y in zip(ta,tb):
        if x==y: continue
        xd=x[0].isdigit(); yd=y[0].isdigit()
        if xd and yd:
            xi,yi=int(x),int(y)
            if xi!=yi: return -1 if xi<yi else 1
        elif xd!=yd:
            return 1 if xd else -1          # numeric segment newer than alpha
        else:
            return -1 if x<y else 1
    if len(ta)==len(tb): return 0
    return 1 if len(ta)>len(tb) else -1
def _lcp(a,b):
    n=min(len(a),len(b)); i=0
    while i<n and a[i]==b[i]: i+=1
    return i
def nearest(cand_dirs, brel):
    """Pick the source dir whose release is closest to brel: longest common
    release-prefix first (keeps the same el9 stream), then the greatest
    release <= brel, else the greatest overall."""
    info=[(_lcp(split_nvr(d)[2],brel), split_nvr(d)[2], d) for d in cand_dirs]
    bl=max(i[0] for i in info)
    top=[(r,d) for l,r,d in info if l==bl]
    le=[(r,d) for r,d in top if _rpmvercmp(r,brel)<=0] or top
    le.sort(key=functools.cmp_to_key(lambda x,y:_rpmvercmp(x[0],y[0])))
    return le[-1][1]

def guess_name(bname):
    """Best-effort source NAME for a binary when rpmspec map missed it."""
    if bname in name2rels: return bname
    parts=bname.split('-')
    for k in range(len(parts)-1,0,-1):      # trim subpackage suffixes: a-b-c -> a-b -> a
        cand='-'.join(parts[:k])
        if cand in name2rels: return cand
    for pre in ("python3-","python-","lib"): # then subpackage prefixes
        if bname.startswith(pre) and bname[len(pre):] in name2rels:
            return bname[len(pre):]
    return None

def resolve_exact(bname, bver, brel):
    """Exact (V,R) source, or None. Disambiguate only genuine (V,R) collisions."""
    cands=vr2src.get((bver,brel),[])
    if len(cands)==1: return cands[0]
    if len(cands)>1:
        if bin2src.get(bname) in cands: return bin2src[bname]
        for cand in cands:
            if bname.startswith(src_name[cand]): return cand
        for cand in cands:
            if guess_name(bname)==src_name[cand]: return cand
        return None
    return None

def resolve_fallback(bname, brel, linked_by_name):
    """No exact (V,R): choose within the binary's SOURCE NAME. Prefer a source
    already linked (exact) for this OCP version (anchor subpackages such as
    device-mapper->lvm2), else the nearest release in the pool. Never crosses
    to a differently-named / arbitrary-stream source the way a flat name map
    would."""
    name=bin2name.get(bname) or guess_name(bname)
    if not name: return None
    anchored=linked_by_name.get(name)
    if anchored: return nearest(anchored, brel)
    pool=name2rels.get(name)
    if pool: return nearest(pool, brel)
    return None

byocp=os.path.join(stage,"by-ocp")
if os.path.exists(byocp):
    import shutil; shutil.rmtree(byocp)
os.makedirs(byocp)
b2s_out=open(os.path.join(stage,"meta","bin-to-src.tsv"),"w")
b2s_out.write("# binary-NEVRA\tsource-NEVR\tocp-version\n")

total=unres=0; per={}
for tsv in sorted(glob.glob(os.path.join(rpmdb,"*.tsv"))):
    ver=os.path.basename(tsv)[:-4]
    vdir=os.path.join(byocp,ver); os.makedirs(vdir,exist_ok=True)
    rows=[]                                  # (nevra,bname,brel)
    for line in open(tsv):
        f=line.rstrip("\n").split("\t")
        if len(f)<5: continue
        bname,epoch,bver,brel,arch=f[:5]
        nevra=f"{bname}-{('' if epoch in ('0','') else epoch+':')}{bver}-{brel}.{arch}"
        rows.append((nevra,bname,bver,brel))
    t=len(rows)
    # Pass 1: exact (V,R). These sources are trusted and anchor pass 2.
    res={}; linked_by_name=collections.defaultdict(list); exact_order=[]
    for i,(nevra,bname,bver,brel) in enumerate(rows):
        s=resolve_exact(bname,bver,brel)
        res[i]=s
        if s:
            if s not in linked_by_name[src_name[s]]:
                linked_by_name[src_name[s]].append(s)
            exact_order.append(s)
    # Pass 2: no exact match -> anchored / nearest-release within source name.
    fb_order=[]
    for i,(nevra,bname,bver,brel) in enumerate(rows):
        if res[i] is None:
            res[i]=resolve_fallback(bname,brel,linked_by_name)
            if res[i]: fb_order.append(res[i])
    # Emit tsv (input order) and count unresolved.
    u=0
    for i,(nevra,bname,bver,brel) in enumerate(rows):
        s=res[i]; b2s_out.write(f"{nevra}\t{s or 'UNRESOLVED'}\t{ver}\n")
        if not s: u+=1
    # Create links: exact-match (lead) sources first so they own the plain
    # name; fallbacks only collide when a genuinely distinct source remains.
    linked=set()
    for src in exact_order+fb_order:
        if src in linked: continue
        linked.add(src)
        name=src_name[src]; link=os.path.join(vdir,name)
        if os.path.lexists(link):           # name collision within a version
            link=os.path.join(vdir,src)
        os.symlink(os.path.join("../../srpms",src), link)
    per[ver]=(t,u,len(linked)); total+=t; unres+=u
b2s_out.close()
print("by-ocp tree built:")
for v in sorted(per):
    t,u,l=per[v]; print(f"  {v}: {l} srpms linked  ({t} binaries, {u} unresolved)")
print(f"  TOTAL: {total} binaries, {unres} unresolved")
PY

log "done — by-ocp/ and meta/bin-to-src.tsv written under $STAGE"
