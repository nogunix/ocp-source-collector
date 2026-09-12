#!/usr/bin/env python3
"""Expand git submodules in an already-staged casket.

Runs on the STAGE tree (after the package script extracted git/<comp>/...), so
one implementation serves Phase A, Phase B (redhat/certified/community
operators) and B-operand alike -- same shape as collect-deps.py, and it runs
just before it so the filled-in submodules get their own deps collected too.

  collect-submodules.py <stage> [--jobs 8] [--max-depth 3]

Why it is needed: a GitHub codeload archive is `git archive` output, and
`git archive` writes a gitlink as an empty directory. Nothing in the tarball
carries the submodule's content or even its pinned commit. Trees that use
submodules therefore reached the caskets as build glue with the code missing.
The pinned commit is read back from the git trees API (mode 160000); when that
is unavailable the .gitmodules branch head is used and the row is marked
APPROX, exactly as phase-b-operand-fetch-source.sh records approximate refs.

Fills, for every empty submodule dir it can resolve:
  <stage>/git/<comp>/<submodule path>/   extracted source of the pinned commit
  <stage>/meta/SUBMODULES.tsv            component | path | url | ref | exact | status
  <stage>/meta/SUBMODULES-uncovered.txt  every submodule that did NOT land, named

Archives cache in <store>/../submodule-cache and extract once into a shared
store (default $CASKET_WORK/submodule-store); staging is hardlinks, so the
store MUST be on the same filesystem as the stage or the links degrade to full
copies -- the same cross-filesystem trap as $CASKET_DEP_STORE.
"""
import argparse
import concurrent.futures as cf
import importlib.util
import io
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import submodulelib as sml  # noqa: E402

UA = "casket-ocp/1.0 (offline source archive)"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _load_index_records():
    """Borrow build-source-index.py's MANIFEST reader (A/B/B-operand formats)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "build-source-index.py")
    spec = importlib.util.spec_from_file_location("_bsi", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.records


def parent_refs(stage):
    """{git dir -> (repo_url, ref)} from the staged MANIFEST, if there is one."""
    mf = os.path.join(stage, "meta", "MANIFEST.json")
    if not os.path.isfile(mf):
        return {}
    try:
        manifest = json.load(open(mf))
        records = _load_index_records()
    except Exception as exc:                  # noqa: BLE001 - degrade, not die
        log(f"WARNING: cannot read {mf} ({type(exc).__name__}: {exc}) — "
            "falling back to branch heads (every row will be APPROX)")
        return {}
    out = {}
    for _comp, repo, ref, _ver, tb in records(manifest):
        d = tb[:-7] if tb.endswith(".tar.gz") else tb
        if d and repo and ref and d not in out:
            out[d] = (repo, ref)
    return out


def fetch(url, timeout=120, retries=3, headers=None):
    last = None
    for attempt in range(retries):
        try:
            hdrs = {"User-Agent": UA}
            hdrs.update(headers or {})
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # 404 is a real answer (repo went private / commit gone); 403 is
            # the rate limit and retrying just burns the budget faster.
            if e.code in (403, 404, 451):
                raise
            last = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
        if attempt + 1 < retries:
            time.sleep(2 * (attempt + 1))
    raise last


def gitlinks(slug, ref, token):
    """Pinned submodule commits for one parent commit, or {} if unavailable."""
    hdrs = {"Accept": "application/vnd.github+json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    try:
        return sml.gitlinks_from_tree(
            json.loads(fetch(sml.tree_api_url(slug, ref), headers=hdrs)))
    except Exception as exc:                  # noqa: BLE001 - reported by caller
        log(f"  ! tree API {slug}@{ref[:12]}: {type(exc).__name__}: {exc}")
        return {}


def extract(blob, target):
    """Unpack a codeload archive, stripping its single top-level dir."""
    tmp = target + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        members = []
        for m in tf.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            # never let an archive escape its target
            if m.name.startswith("/") or ".." in m.name.split("/"):
                continue
            members.append(m)
        tf.extractall(tmp, members=members)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)


def store_dir(pin):
    """Store path for a pin. Refs can be branches ("release/v1.14.7"), so the
    slash has to go or the store grows phantom directory levels."""
    return f"{pin.slug.replace('/', '_')}@{pin.ref.replace('/', '_')}"


def acquire(pin, store, cache):
    """Fetch + extract one submodule into the shared store. Never raises."""
    dest = store_dir(pin)
    target = os.path.join(store, dest)
    try:
        if os.path.isdir(target) and os.listdir(target):
            return dest, "ok-cached"
        cpath = os.path.join(cache, dest + ".tar.gz")
        if os.path.exists(cpath):
            blob = open(cpath, "rb").read()
        else:
            blob = fetch(pin.url)
            tmp = f"{cpath}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, cpath)
        extract(blob, target)
        return dest, "ok"
    except Exception as e:                    # noqa: BLE001 - reported, not raised
        return dest, f"failed:{type(e).__name__}"


def link_tree(src, dst):
    """cp -al: hardlink the store into the stage (no second copy on disk)."""
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        out = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(out, exist_ok=True)
        for fn in filenames:
            d = os.path.join(out, fn)
            if not os.path.exists(d):
                try:
                    os.link(os.path.join(dirpath, fn), d)
                except OSError:
                    shutil.copy2(os.path.join(dirpath, fn), d)


def scan_one(tree_root, comp, rel, slug, ref, token, seen):
    """Plan the submodules of ONE directory that has a .gitmodules.

    Returns (todo, bad) where todo rows are (comp, rel_path_from_comp, pin).
    """
    gm = os.path.join(tree_root, ".gitmodules")
    if not os.path.isfile(gm):
        return [], []
    try:
        entries = sml.parse_gitmodules(
            open(gm, encoding="utf-8", errors="replace").read())
    except OSError as exc:
        return [], [(comp, os.path.join(rel, ".gitmodules"), "-",
                     f"unreadable:{type(exc).__name__}")]
    if not entries:
        return [], []

    links = {}
    if slug and ref:
        if (slug, ref) not in seen:
            seen[(slug, ref)] = gitlinks(slug, ref, token)
        links = seen[(slug, ref)]

    pins, skipped = sml.plan_submodules(entries, links, slug)
    bad = [(comp, os.path.join(rel, p), u, why) for p, u, why in skipped]
    todo = []
    for pin in pins:
        d = os.path.join(tree_root, pin.path)
        # Only fill what the archive left empty; some trees really do ship a
        # populated submodule dir, and there the archive is the authority.
        if os.path.isdir(d) and os.listdir(d):
            continue
        todo.append((comp, os.path.join(rel, pin.path), pin))
    return todo, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage")
    ap.add_argument("--out", default=None,
                    help="write into this tree instead of <stage> "
                         "(overlayfs upperdir, when backfilling a built casket)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max-depth", type=int, default=3,
                    help="nested submodule recursion cap")
    ap.add_argument("--store", default=os.environ.get(
        "CASKET_SUBMODULE_STORE",
        os.path.join(os.environ.get("CASKET_WORK", os.path.expanduser("~/casket-work")),
                     "submodule-store")))
    args = ap.parse_args()

    stage = os.path.abspath(args.stage)
    out = os.path.abspath(args.out) if args.out else stage
    gitroot = os.path.join(stage, "git")
    if not os.path.isdir(gitroot):
        log(f"no {gitroot} — nothing to do")
        return 0

    store = args.store
    cache = os.environ.get("CASKET_SUBMODULE_CACHE") or store.rstrip("/") + "-cache"
    # Same trap as CASKET_DEP_STORE: a systemd unit or systemd-run job starts
    # from a minimal environment, so an unset var goes unnoticed exactly where
    # nobody is watching, and the store lands inside the repo on the root fs.
    if not os.environ.get("CASKET_SUBMODULE_STORE"):
        log(f"WARNING: CASKET_SUBMODULE_STORE unset — using {store}")
        log("WARNING:   put it on the SAME filesystem as the stage or the "
            "store->stage hardlinks degrade to full copies")
    os.makedirs(store, exist_ok=True)
    os.makedirs(cache, exist_ok=True)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        # 60 req/h unauthenticated; a single operators casket needs ~70 tree
        # calls. Without a token most rows silently fall back to branch heads
        # and the casket records APPROX for commits we could have had exactly.
        log("WARNING: no GITHUB_TOKEN/GH_TOKEN — the git trees API is limited to "
            "60 requests/hour, so most submodules will fall back to branch "
            "heads and be recorded exact=0 (APPROX)")

    # ---- walk in rounds: a submodule can itself have submodules, so each
    # round re-scans only what the previous round just filled (cap: --max-depth)
    all_todo, bad, seen, status = [], [], {}, {}
    refs = parent_refs(stage)
    # dir on disk -> (owner slug, ref) whose tree object holds its gitlinks
    frontier = []
    for comp in sorted(os.listdir(gitroot)):
        tree = os.path.join(gitroot, comp)
        if not os.path.isdir(tree) or os.path.islink(tree):
            continue
        repo, ref = refs.get(comp, ("", ""))
        frontier.append((tree, comp, "", sml.github_slug(repo) if repo else "", ref))

    for depth in range(1, args.max_depth + 1):
        todo = []
        for tree, comp, rel, slug, ref in frontier:
            # Per-tree guard: one unreadable .gitmodules costs only itself.
            try:
                t_, b_ = scan_one(tree, comp, rel, slug, ref, token, seen)
                todo.extend(t_)
                bad.extend(b_)
            except Exception as exc:          # noqa: BLE001 - reported, not raised
                bad.append((comp, rel or ".", "-",
                            f"scan-failed:{type(exc).__name__}: {exc}"))
                log(f"  ! {comp}/{rel}: scan failed "
                    f"({type(exc).__name__}: {exc}) — continuing")
        if not todo:
            break
        exact = sum(1 for _c, _r, p in todo if p.exact)
        log(f"depth {depth}: {len(todo)} empty submodule(s) in "
            f"{len({c for c, _r, _p in todo})} tree(s): {exact} pinned exactly, "
            f"{len(todo) - exact} branch-head (APPROX)")

        # fetch + extract into the shared store, once per (repo, ref)
        uniq = {}
        for _comp, _rel, pin in todo:
            uniq.setdefault(f"{pin.slug}@{pin.ref}", pin)
        uniq = {k: v for k, v in uniq.items() if k not in status}
        if uniq:
            with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
                futs = {ex.submit(acquire, pin, store, cache): key
                        for key, pin in uniq.items()}
                for i, fut in enumerate(cf.as_completed(futs), 1):
                    _dest, st = fut.result()
                    status[futs[fut]] = st
                    if i % 50 == 0:
                        log(f"  {i}/{len(uniq)}")
            ok = sum(1 for k in uniq if status.get(k, "").startswith("ok"))
            log(f"  acquired {ok}/{len(uniq)} unique submodule archives")

        # hardlink into the stage at each referencing path
        frontier = []
        for comp, rel, pin in todo:
            key = f"{pin.slug}@{pin.ref}"
            if not status.get(key, "").startswith("ok"):
                continue
            dst = os.path.join(out, "git", comp, rel)
            try:
                link_tree(os.path.join(store, store_dir(pin)), dst)
            except Exception as exc:          # noqa: BLE001
                bad.append((comp, rel, pin.url, f"link-failed:{type(exc).__name__}"))
                continue
            # a filled submodule may carry submodules of its own
            frontier.append((dst, comp, rel, pin.slug, pin.ref))
        all_todo.extend(todo)

    if not all_todo and not bad:
        log("no unexpanded submodules found")
        return 0
    landed = sum(1 for _c, _r, p in all_todo
                 if status.get(f"{p.slug}@{p.ref}", "").startswith("ok"))
    log(f"filled {landed}/{len(all_todo)} submodule dirs")

    # ---- meta/SUBMODULES.tsv + uncovered (report landed AND missing, always)
    meta = os.path.join(out, "meta")
    os.makedirs(meta, exist_ok=True)
    with open(os.path.join(meta, "SUBMODULES.tsv"), "w") as f:
        f.write("# component\tpath\trepo\tref\texact\tstatus\n")
        for comp, rel, pin in sorted(all_todo, key=lambda r: (r[0], r[1])):
            f.write(f"{comp}\t{rel}\t{pin.slug}\t{pin.ref}\t"
                    f"{1 if pin.exact else 0}\t"
                    f"{status.get(f'{pin.slug}@{pin.ref}', 'unresolved')}\n")
    missing = [(c, r, p) for c, r, p in all_todo
               if not status.get(f"{p.slug}@{p.ref}", "").startswith("ok")]
    unc = os.path.join(meta, "SUBMODULES-uncovered.txt")
    if missing or bad:
        with open(unc, "w") as f:
            f.write(f"# submodules that did NOT land "
                    f"({len(missing)}/{len(all_todo)})\n")
            for comp, rel, pin in sorted(missing, key=lambda r: (r[0], r[1])):
                f.write(f"{comp}\t{rel}\t{pin.slug}\t{pin.ref}\t"
                        f"{status.get(f'{pin.slug}@{pin.ref}', 'unresolved')}\n")
            f.write(f"\n# submodules with no fetchable GitHub source ({len(bad)})\n"
                    "# not-github: the submodule lives elsewhere (gitlab etc)\n"
                    "# no-pinned-commit: no mode-160000 entry and no branch to fall back to\n")
            for row in sorted(set(bad)):
                f.write("\t".join(row) + "\n")
    elif os.path.exists(unc):
        os.remove(unc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
