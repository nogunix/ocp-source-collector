#!/usr/bin/env python3
"""Collect language-level dependency sources for an already-staged casket.

Runs on the STAGE tree (after the package script extracted git/<comp>/...), so
one implementation serves every phase: Phase A, Phase B (redhat/certified/
community operators) and B-operand all stage the same way.

  collect-deps.py <stage> [--jobs 12] [--eco go,crates,npm,pypi]

For every source tree under <stage>/git/ it resolves the lockfiles (see
deplib.py) and fills:

  <stage>/deps/go/<module>@<version>/      Go module zips from proxy.golang.org
  <stage>/deps/crates/<name>-<version>/    crates.io .crate archives
  <stage>/deps/npm/<name>@<version>/       tarballs named by package-lock
  <stage>/deps/pypi/<name>-<version>/      PyPI sdists (wheel only as a fallback)
  <stage>/meta/DEPS.tsv                    component | eco | name | version | dir | status
  <stage>/meta/DEPS-uncovered.txt          everything that did NOT land, named

Trees that already carry their deps (Go/Rust vendor/) are skipped -- the point
is the ones that do not.

Downloads cache in <store>/../dep-cache and extract once into a shared store
(default $CASKET_WORK/dep-store); staging is hardlinks, so re-packaging a minor
costs no extra disk and mksquashfs still stores shared deps once.
"""
import argparse
import concurrent.futures as cf
import io
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

# This checkout (parent of scripts/), not $HOME: the repo directory is
# renameable, and under sudo $HOME is /root.
CASKET_WORK = os.environ.get("CASKET_WORK") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deplib  # noqa: E402

UA = "casket-ocp/1.0 (offline source archive)"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def fetch(url, timeout=120, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last = e
            # 404 is a real answer (yanked/renamed package), not worth retrying
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                break
            time.sleep(1 + attempt)
    raise last


def pypi_sdist_url(name, version):
    """PyPI file URLs are hash-pathed, so the JSON API is the only way in.

    Prefer the sdist; fall back to a pure-python wheel (still real .py source)
    and record which one it was.
    """
    data = json.loads(fetch(PYPI_JSON.format(name=name, version=version)))
    urls = data.get("urls") or []
    for u in urls:
        if u.get("packagetype") == "sdist":
            return u["url"], "sdist"
    for u in urls:
        if u.get("packagetype") == "bdist_wheel" and u.get("filename", "").endswith("-none-any.whl"):
            return u["url"], "wheel"
    raise ValueError("no sdist or pure wheel")


def extract(blob, dest, strip, url):
    """Unpack an archive into dest (atomically via a tmp dir).

    strip="go": a module zip has every entry under "<module>@<version>/", which
    contains slashes, so a plain strip-1 would leave github.com/... in the way.
    strip="1": drop one leading directory (crate/npm/sdist convention).
    """
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        if url.endswith(".zip") or strip == "go":
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = [n for n in z.namelist() if n]
                prefix = ""
                if strip == "go" and names:
                    # entries are "<module>@<version>/<path>" and <module> has
                    # slashes of its own, so cut at the first "/" AFTER the "@"
                    first = names[0]
                    at = first.find("@")
                    slash = first.find("/", at) if at >= 0 else -1
                    if slash > 0:
                        prefix = first[:slash + 1]
                elif strip == "1" and names:
                    prefix = names[0].split("/", 1)[0] + "/"
                for n in names:
                    if n.endswith("/"):
                        continue
                    rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
                    if not rel or rel.startswith("/") or ".." in rel.split("/"):
                        continue
                    out = os.path.join(tmp, rel)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with z.open(n) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            mode = "r:*"
            with tarfile.open(fileobj=io.BytesIO(blob), mode=mode) as t:
                for m in t.getmembers():
                    if not (m.isfile() or m.isdir()):
                        continue
                    parts = m.name.split("/")
                    rel = "/".join(parts[1:]) if strip == "1" else m.name
                    if not rel or ".." in parts:
                        continue
                    out = os.path.join(tmp, rel)
                    if m.isdir():
                        os.makedirs(out, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    src = t.extractfile(m)
                    if src is None:
                        continue
                    with open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        os.replace(tmp, dest)
        return True
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def acquire(dep, store, cache):
    """Download (cached) + extract one dep into the shared store."""
    eco, name, version, url, dest, strip = dep
    target = os.path.join(store, dest)
    if os.path.isdir(target):
        return dest, "ok", ""
    if not dest or not url:
        # non-github git dep, or a source string we don't know how to fetch
        return dest, "unsupported", url
    kind = "sdist"
    try:
        if eco == "pypi" and url == "pypi-api":
            url, kind = pypi_sdist_url(name, version)
        cache_key = dest.replace("/", "_") + (".zip" if strip == "go" else ".tar.gz")
        cpath = os.path.join(cache, cache_key)
        if os.path.exists(cpath):
            blob = open(cpath, "rb").read()
        else:
            blob = fetch(url)
            tmp = f"{cpath}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, cpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        extract(blob, target, strip, url)
        return dest, ("ok" if kind == "sdist" else "ok-wheel"), url
    except Exception as e:
        return dest, f"failed:{type(e).__name__}", url


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage")
    ap.add_argument("--out", default=None,
                    help="write deps/ + meta/ here instead of into <stage> "
                         "(overlayfs upperdir, when backfilling a built casket)")
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--eco", default="go,crates,npm,pypi",
                    help="comma-separated ecosystems to collect")
    ap.add_argument("--store", default=os.environ.get(
        "CASKET_DEP_STORE",
        os.path.join(CASKET_WORK,
                     "dep-store")))
    ap.add_argument("--max-depth", type=int, default=2)
    args = ap.parse_args()

    stage = os.path.abspath(args.stage)
    out = os.path.abspath(args.out) if args.out else stage
    gitroot = os.path.join(stage, "git")
    if not os.path.isdir(gitroot):
        log(f"no {gitroot} — nothing to do")
        return 0
    ecos = {e.strip() for e in args.eco.split(",") if e.strip()}
    store = args.store
    # sibling of the store by default; both are pure cache (regenerable), which
    # is why they can live on the HDD while the stage lives on the SSD
    cache = os.environ.get("CASKET_DEP_CACHE") or store.rstrip("/") + "-cache"
    # The fallback keeps this portable (no host path baked in), but it is silent
    # and it lands the store INSIDE the repo on the root fs. A systemd unit or a
    # systemd-run job starts from a minimal environment, so the var goes missing
    # exactly where nobody is watching: the 2026-07-31 auto-update run wrote 160G
    # into the repo checkout (then named ~/casket-work) and re-downloaded
    # every archive that
    # /mnt/hdd/casket-dep-store already held. Say so loudly rather than
    # hardcoding a path.
    if not os.environ.get("CASKET_DEP_STORE"):
        log(f"WARNING: CASKET_DEP_STORE unset — using {store}")
        log("WARNING:   this is regenerable cache and can reach 150G+; point it at "
            "persistent storage (production: /mnt/hdd/casket-dep-store) or the "
            "shared cache is bypassed and everything is re-downloaded")
    os.makedirs(store, exist_ok=True)
    os.makedirs(cache, exist_ok=True)

    # ---- resolve every tree
    rows = []          # (component, manifest, dep)
    barren = []        # (component, manifest) — found, but nothing fetchable in it
    broken = []        # (component, manifest, reason) — resolver/walk blew up
    for comp in sorted(os.listdir(gitroot)):
        tree = os.path.join(gitroot, comp)
        if not os.path.isdir(tree) or os.path.islink(tree):
            continue
        # Per-tree guard on top of the per-manifest one in scan_tree: a walk
        # error (unreadable dir, decode failure) must cost this tree only.
        try:
            for manifest, dep in deplib.scan_tree(tree, max_depth=args.max_depth):
                if dep is None:
                    barren.append((comp, manifest))
                elif isinstance(dep, BaseException):
                    broken.append((comp, manifest, f"{type(dep).__name__}: {dep}"))
                elif dep[0] in ecos:
                    rows.append((comp, manifest, dep))
        except Exception as exc:              # noqa: BLE001 - reported, not raised
            broken.append((comp, "-", f"{type(exc).__name__}: {exc}"))
            log(f"  ! {comp}: scan failed ({type(exc).__name__}: {exc}) — continuing")
    if broken:
        log(f"{len(broken)} manifest(s)/tree(s) failed to parse — named in "
            f"meta/DEPS-uncovered.txt, collection continues")
    if not rows:
        log(f"no resolvable dependency manifests found "
            f"({len(barren)} unresolvable, {len(broken)} broken)")
        return 0

    uniq = {}
    for _comp, _man, dep in rows:
        uniq.setdefault(dep[4] or f"?{dep[0]}/{dep[1]}", dep)
    log(f"{len(rows)} dep refs from {len({r[0] for r in rows})} tree(s) "
        f"-> {len(uniq)} unique archives")

    # ---- fetch + extract into the shared store
    status = {}
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(acquire, dep, store, cache): dest
                for dest, dep in uniq.items()}
        done = 0
        for fut in cf.as_completed(futs):
            dest, st, _url = fut.result()
            status[dest] = st
            done += 1
            if done % 500 == 0:
                log(f"  {done}/{len(uniq)}")

    ok = sum(1 for s in status.values() if s.startswith("ok"))
    log(f"acquired {ok}/{len(uniq)} archives")

    # ---- hardlink into the stage
    for dest, st in status.items():
        if not st.startswith("ok"):
            continue
        link_tree(os.path.join(store, dest), os.path.join(out, "deps", dest))

    # ---- meta/DEPS.tsv + uncovered (report resolved AND missing, always)
    meta = os.path.join(out, "meta")
    os.makedirs(meta, exist_ok=True)
    with open(os.path.join(meta, "DEPS.tsv"), "w") as f:
        f.write("# component\tmanifest\tecosystem\tname\tversion\tdir\tstatus\n")
        for comp, man, dep in sorted(rows):
            eco, name, version, _url, dest, _strip = dep
            f.write(f"{comp}\t{man}\t{eco}\t{name}\t{version}\t{dest or '-'}\t"
                    f"{status.get(dest, 'unresolved')}\n")
    bad = [(c, m, d) for c, m, d in rows if not status.get(d[4], "").startswith("ok")]
    unc = os.path.join(meta, "DEPS-uncovered.txt")
    if bad or barren or broken:
        with open(unc, "w") as f:
            f.write(f"# dependency refs that did NOT land in deps/ ({len(bad)}/{len(rows)})\n")
            for comp, man, dep in sorted(bad):
                f.write(f"{comp}\t{man}\t{dep[0]}\t{dep[1]}\t{dep[2]}\t"
                        f"{status.get(dep[4], 'unresolved')}\n")
            f.write(f"\n# manifests found but not resolvable to downloads ({len(barren)})\n"
                    "# typically unpinned requirements (>=, or bare names) — there is no\n"
                    "# single correct version to fetch offline\n")
            for comp, man in sorted(set(barren)):
                f.write(f"{comp}\t{man}\tno-pinned-versions\n")
            f.write(f"\n# manifests the resolver could not parse ({len(broken)})\n"
                    "# these cost only their own deps -- the rest of the casket is\n"
                    "# unaffected (a single bad package-lock used to abort everything)\n")
            for comp, man, why in sorted(set(broken)):
                f.write(f"{comp}\t{man}\t{why}\n")
    elif os.path.exists(unc):
        os.remove(unc)

    per_eco = {}
    for dest, st in status.items():
        eco = dest.split("/", 1)[0]
        per_eco.setdefault(eco, [0, 0])
        per_eco[eco][0] += 1
        per_eco[eco][1] += 1 if st.startswith("ok") else 0
    log("deps: " + ", ".join(f"{e} {v[1]}/{v[0]}" for e, v in sorted(per_eco.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
