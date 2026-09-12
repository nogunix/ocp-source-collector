#!/usr/bin/env python3
"""Fetch SRPMs straight from the Red Hat CDN, using the entitlement cert.

Replaces the per-package `dnf download --source --releasever=N` sweep that
collect.sh used for the EUS/E4S leftovers. That sweep ran one dnf per package
per releasever -- up to 475 x 5 invocations -- and every invocation re-resolved
the metadata of fourteen repositories. On the VM the dnf cache absorbed that;
in a --rm container nothing is cached and the 2026-08-25 run spent 15 hours
without downloading a single new package.

Here the metadata is fetched ONCE per (repo, releasever) that is actually
needed, turned into a local (name, version, release) -> URL index, and the
packages are pulled directly. Roughly a dozen metadata fetches plus one GET
per package.

It also removes the hardcoded `9.0 9.2 9.4 9.6 9.8` list. The releasever a
package needs is written on the package: nvr ending in .el9_2 is 9.2. Only
those releasevers are consulted, and a RHEL 9.10 (or an el10 tag) is handled
without editing anything.

Repos, their $releasever-templated baseurls and the client cert paths all come
from /etc/yum.repos.d/redhat.repo, which subscription-manager generates from
the product certificate -- so this stays correct as entitlements change.

Usage:
  cdn-fetch.py --missing missing.txt --destdir DIR [--repo-file FILE]
               [--jobs N] [--dry-run]

Exit status is 0 even when packages are unresolvable: "not on the CDN" is a
real answer for an aged-out micro-release, and the caller records it.
"""
import argparse
import concurrent.futures as cf
import configparser
import gzip
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

COMMON_NS = "{http://linux.duke.edu/metadata/common}"
REPO_NS = "{http://linux.duke.edu/metadata/repo}"

# foo-1.2.3-4.el9_2  /  foo-1.2.3-4.el9_2.1  /  foo-1.2.3-4.el9
NVR_RE = re.compile(r'^(?P<name>.+)-(?P<version>[^-]+)-(?P<release>[^-]+)$')
DIST_RE = re.compile(r'\.el(?P<major>\d+)(?:_(?P<minor>\d+))?')


def log(msg):
    print(f"[cdn-fetch] {msg}", file=sys.stderr, flush=True)


def releasever_for(release):
    """`4.el9_2` -> "9.2";  `4.el9` -> "" (plain repos, no pin needed)."""
    m = DIST_RE.search(release)
    if not m:
        return None
    if m.group("minor") is None:
        return ""
    return f'{m.group("major")}.{m.group("minor")}'


def parse_nvrs(path):
    out = []
    with open(path) as f:
        for line in f:
            spec = line.strip()
            if not spec or spec.startswith("#"):
                continue
            spec = spec.removesuffix(".src.rpm").removesuffix(".src")
            m = NVR_RE.match(spec)
            if m:
                out.append((m.group("name"), m.group("version"), m.group("release")))
    return out


def repo_patterns(majors):
    """Repo-id patterns worth indexing, derived from the RHEL majors in play.

    An entitlement enables ~920 source repos (satellite, service-interconnect,
    ansible, ...). Indexing all of them took 31 minutes for 30 packages, and
    every one outside this set contributed "+0 new". These are the repos
    collect.sh enables, i.e. the ones a node-OS package can actually come from.
    Built per major so an el10 dist tag reaches rhel-10 repos without an edit.
    """
    pats = []
    for m in sorted(majors):
        pats += [
            rf"^rhel-{m}-for-x86_64-(baseos|appstream|highavailability|"
            rf"resilientstorage|nfv|rt)-(eus-|e4s-)?source-rpms$",
            rf"^codeready-builder-for-rhel-{m}-x86_64-(eus-|e4s-)?source-rpms$",
            rf"^fast-datapath-for-rhel-{m}-x86_64-source-rpms$",
            rf"^rhocp-[0-9.]+-for-rhel-{m}-x86_64-source-rpms$",
        ]
    return [re.compile(p) for p in pats]


def read_repos(path, patterns=None):
    """repoid -> dict(baseurl, cert, key, cacert) for the source repos we want."""
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(path)
    repos = {}
    for sec in cp.sections():
        if not sec.endswith("-source-rpms"):
            continue
        if patterns and not any(p.match(sec) for p in patterns):
            continue
        base = cp[sec].get("baseurl", "").strip()
        if not base:
            continue
        repos[sec] = {
            "baseurl": base,
            "cert": cp[sec].get("sslclientcert", "").strip(),
            "key": cp[sec].get("sslclientkey", "").strip(),
            "cacert": cp[sec].get("sslcacert", "").strip(),
        }
    return repos


def opener_for(repo):
    ctx = ssl.create_default_context(cafile=repo["cacert"] or None)
    if repo["cert"] and repo["key"]:
        ctx.load_cert_chain(repo["cert"], repo["key"])
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def fetch(op, url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "casket-ocp/1.0"})
    with op.open(req, timeout=timeout) as r:
        return r.read()


def index_repo(repo, releasever):
    """(name, version, release) -> absolute URL, for one repo at one releasever."""
    base = repo["baseurl"].replace("$releasever", releasever) if releasever else repo["baseurl"]
    if "$releasever" in base:          # plain repos still template it
        base = base.replace("$releasever", "9")
    op = opener_for(repo)
    repomd = ET.fromstring(fetch(op, f"{base}/repodata/repomd.xml"))
    href = None
    for data in repomd.findall(f"{REPO_NS}data"):
        if data.get("type") == "primary":
            loc = data.find(f"{REPO_NS}location")
            href = loc.get("href") if loc is not None else None
            break
    if not href:
        raise RuntimeError("no primary in repomd.xml")
    blob = fetch(op, f"{base}/{href}")
    if href.endswith(".gz"):
        blob = gzip.decompress(blob)
    out = {}
    root = ET.fromstring(blob)
    for pkg in root.findall(f"{COMMON_NS}package"):
        name = pkg.findtext(f"{COMMON_NS}name")
        ver = pkg.find(f"{COMMON_NS}version")
        loc = pkg.find(f"{COMMON_NS}location")
        if name is None or ver is None or loc is None:
            continue
        out[(name, ver.get("ver"), ver.get("rel"))] = f'{base}/{loc.get("href")}'
    return out


def download(args):
    url, dest, repo = args
    tmp = dest + ".partial"
    try:
        op = opener_for(repo)
        blob = fetch(op, url)
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, dest)
        return dest, None
    except Exception as e:                     # noqa: BLE001 - reported, not raised
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return dest, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", required=True)
    ap.add_argument("--destdir", required=True)
    ap.add_argument("--repo-file", default="/etc/yum.repos.d/redhat.repo")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all-repos", action="store_true",
                    help="index every entitled *-source-rpms repo (~920); "
                         "the default set is the ones collect.sh enables")
    args = ap.parse_args()

    nvrs = parse_nvrs(args.missing)
    if not nvrs:
        log("nothing to fetch")
        return 0
    # Which releasevers -- and which RHEL majors -- do the wanted packages
    # actually call for? Both are read off the packages themselves.
    wanted, majors = {}, set()
    for nvr in nvrs:
        rv = releasever_for(nvr[2])
        if rv is None:
            continue
        wanted.setdefault(rv, []).append(nvr)
        m = DIST_RE.search(nvr[2])
        if m:
            majors.add(m.group("major"))
    if not wanted:
        log("no packages carry a recognisable dist tag")
        return 0

    pats = None if args.all_repos else repo_patterns(majors)
    repos = read_repos(args.repo_file, pats)
    if not repos:
        log(f"no matching source repos in {args.repo_file} -- registered? "
            f"(majors seen: {','.join(sorted(majors)) or 'none'})")
        return 1
    log(f"{len(nvrs)} packages, releasevers needed: "
        + ", ".join(f'{k or "(none)"}={len(v)}' for k, v in sorted(wanted.items())))
    log(f"{len(repos)} source repos selected (majors {','.join(sorted(majors))})")

    # Index only the (repo, releasever) combinations that are needed. This is
    # the whole saving: metadata is fetched once instead of once per package.
    # Tried an early exit once every package for a releasever was located
    # (stop once nothing is "still wanted"). Measured worse, not better:
    # 10m44s -> 12m54s on the same 475 packages, same 167 found. The reason is
    # in the numbers themselves -- a good chunk of the "missing" set is not on
    # the CDN at ANY repo (308 of 475 here), so "outstanding" never empties and
    # every repo gets indexed regardless. Kept as a straight index-everything
    # loop; only the per-repo running count survives from that attempt, since
    # it is what showed the early exit could not fire.
    index = {}
    seen_urls = set()
    for rv in sorted(wanted):
        outstanding = {nvr for nvr in wanted[rv] if nvr not in index}
        for rid, repo in sorted(repos.items()):
            # A repo whose baseurl has no $releasever resolves to the same URL
            # for every releasever; index it once.
            resolved = repo["baseurl"].replace("$releasever", rv or "9")
            if resolved in seen_urls:
                continue
            seen_urls.add(resolved)
            try:
                got = index_repo(repo, rv)
            except Exception as e:             # noqa: BLE001
                # A repo that does not exist at this releasever is normal.
                log(f"  skip {rid} @ {rv or 'default'}: {type(e).__name__}")
                continue
            new = 0
            for k, url in got.items():
                if k not in index:
                    index[k] = (url, repo)
                    new += 1
            outstanding -= got.keys()
            log(f"  {rid} @ {rv or 'default'}: {len(got)} pkgs "
                f"(+{new} new, {len(outstanding)} still wanted)")
        if outstanding:
            log(f"  {rv or 'default'}: {len(outstanding)} not found in any repo")
    log(f"index holds {len(index)} source packages")

    os.makedirs(args.destdir, exist_ok=True)
    jobs, unresolved = [], []
    for name, ver, rel in nvrs:
        hit = index.get((name, ver, rel))
        if not hit:
            unresolved.append(f"{name}-{ver}-{rel}")
            continue
        url, repo = hit
        dest = os.path.join(args.destdir, os.path.basename(url))
        if os.path.exists(dest):
            continue
        jobs.append((url, dest, repo))

    log(f"{len(jobs)} to download, {len(unresolved)} not present on the CDN")
    if args.dry_run:
        for u, d, _ in jobs[:10]:
            log(f"  would fetch {os.path.basename(d)}")
        return 0

    ok = failed = 0
    if jobs:
        with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
            for dest, err in ex.map(download, jobs):
                if err:
                    failed += 1
                    log(f"  FAIL {os.path.basename(dest)}: {err}")
                else:
                    ok += 1
    log(f"downloaded {ok}, failed {failed}, unresolved {len(unresolved)}")
    if unresolved:
        p = os.path.join(args.destdir, "..", "cdn-unresolved.txt")
        with open(os.path.abspath(p), "w") as f:
            f.write("# not present in any entitled source repo at the releasever\n")
            f.write("# their own dist tag implies (aged-out micro-releases, mostly)\n")
            for u in sorted(unresolved):
                f.write(u + "\n")
        log(f"unresolved list: {os.path.abspath(p)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
