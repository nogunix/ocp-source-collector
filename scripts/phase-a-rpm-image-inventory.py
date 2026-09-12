#!/usr/bin/env python3
"""Container-RPM inventory: enumerate every container image the caskets know
about (phase a payload images.tsv, phase b containers.tsv, phase b-operand
images.tsv), fetch each image's RPM manifest from the public Pyxis API
(catalog.redhat.com — no image pull, no auth), and report the SRPM set with
its overlap against the existing a-rpm pool.

This is the "one level deeper" read for the RPM layer INSIDE container
images, sibling to a-rpm's rhel-coreos rpmdb read (the node OS layer):
virt-launcher carries qemu/libvirt RPMs, ODF carries ceph RPMs, etc.

Digest lookup order (verified 2026-07-17):
  repositories.manifest_list_digest  — registry.redhat.io multi-arch lists
  image_id                           — quay payload per-arch digests
  repositories.manifest_schema2_digest

Outputs under $CASKET_WORK/phase-a-rpm/:
  pyxis-cache/<digest>.json    raw rpm-manifest (idempotent re-runs are free)
  image-rpms.tsv               digest | repo_hint | srpm_nevr (unique pairs)
  image-rpms-missing.txt       srpm NEVRs absent from the srpms/ pool
  image-rpms-unresolved.tsv    digest | repo_hint | reason
Usage: phase-a-rpm-image-inventory.py [--limit N] [--pool DIR]
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# This checkout (parent of scripts/), not $HOME: the repo directory is
# renameable, and under sudo $HOME is /root.
CASKET_WORK = os.environ.get("CASKET_WORK") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
POOL = "/srv/sources-ocp-srpms/srpms"
PYXIS = "https://catalog.redhat.com/api/containers/v1"
OUT_DIR = os.path.join(CASKET_WORK, "phase-a-rpm")
CACHE = os.path.join(OUT_DIR, "pyxis-cache")
DELAY = 0.15  # be polite to the public API


def gather_images() -> dict[str, str]:
    """digest -> repo hint, from every images/containers tsv on disk."""
    out: dict[str, str] = {}

    def add(pullspec: str):
        if "@sha256:" not in pullspec:
            return
        repo, digest = pullspec.rsplit("@", 1)
        out.setdefault(digest, repo)

    # phase a payload: ocp<ver>/00-discover/images.tsv  (name \t pullspec ...)
    for tsv in glob.glob(f"{CASKET_WORK}/ocp*/00-discover/images.tsv"):
        for line in open(tsv):
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 2:
                add(cols[1])
    # phase b operators: containers.tsv (operator \t bundle \t containerImage ...)
    for tsv in glob.glob(f"{CASKET_WORK}/phase-b*/*/00-discover/containers*.tsv"):
        for line in open(tsv):
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 3:
                add(cols[2])
    # phase b-operand: images.tsv (component \t pullspec)
    for tsv in glob.glob(f"{CASKET_WORK}/phase-b-operand/*/*/00-discover/images.tsv"):
        for line in open(tsv):
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 2:
                add(cols[1])
    return out


def api(path: str) -> dict:
    req = urllib.request.Request(PYXIS + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def find_image_id(digest: str) -> str | None:
    filters = [
        f"repositories.manifest_list_digest=={digest};architecture==amd64",
        f"image_id=={digest}",
        f"repositories.manifest_schema2_digest=={digest};architecture==amd64",
        f"repositories.manifest_list_digest=={digest}",
        f"repositories.manifest_schema2_digest=={digest}",
    ]
    for f in filters:
        try:
            d = api(f"/images?filter={urllib.parse.quote(f, safe='=;,')}"
                    "&page_size=1&include=data._id")
        except Exception:
            time.sleep(1)
            continue
        if d.get("data"):
            return d["data"][0]["_id"]
        time.sleep(DELAY)
    return None


def rpm_manifest(digest: str) -> dict | None:
    cpath = os.path.join(CACHE, digest.replace(":", "_") + ".json")
    if os.path.isfile(cpath):
        try:
            return json.load(open(cpath))
        except Exception:
            pass
    iid = find_image_id(digest)
    if not iid:
        return None
    try:
        d = api(f"/images/id/{iid}/rpm-manifest?include=rpms.srpm_name,rpms.nvra")
    except Exception:
        return None
    json.dump(d, open(cpath, "w"))
    time.sleep(DELAY)
    return d


def main() -> int:
    limit = 0
    pool_dir = POOL
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--limit":
            limit = int(args.pop(0))
        elif a == "--pool":
            pool_dir = args.pop(0)
    os.makedirs(CACHE, exist_ok=True)

    images = gather_images()
    print(f"[inventory] unique image digests: {len(images)}", flush=True)
    items = sorted(images.items())
    if limit:
        items = items[:limit]

    pool = set(os.listdir(pool_dir)) if os.path.isdir(pool_dir) else set()
    pairs: set[tuple[str, str, str]] = set()
    srpms: set[str] = set()
    unresolved: list[tuple[str, str]] = []
    for i, (digest, repo) in enumerate(items, 1):
        m = rpm_manifest(digest)
        if not m:
            unresolved.append((digest, repo))
        else:
            for r in m.get("rpms", []):
                s = r.get("srpm_name") or ""
                if s.endswith(".src.rpm"):
                    nevr = s[:-8]
                    srpms.add(nevr)
                    pairs.add((digest, repo, nevr))
        if i % 100 == 0:
            print(f"[inventory] {i}/{len(items)} "
                  f"(srpms={len(srpms)}, unresolved={len(unresolved)})", flush=True)

    with open(os.path.join(OUT_DIR, "image-rpms.tsv"), "w") as f:
        f.write("# image_digest\trepo\tsrpm_nevr\n")
        for row in sorted(pairs):
            f.write("\t".join(row) + "\n")
    missing = sorted(s for s in srpms if s not in pool)
    with open(os.path.join(OUT_DIR, "image-rpms-missing.txt"), "w") as f:
        f.writelines(s + "\n" for s in missing)
    with open(os.path.join(OUT_DIR, "image-rpms-unresolved.tsv"), "w") as f:
        f.writelines(f"{d}\t{r}\n" for d, r in unresolved)

    print(f"[inventory] images resolved: {len(items) - len(unresolved)}/{len(items)}")
    print(f"[inventory] unique SRPMs across all images: {len(srpms)}")
    print(f"[inventory] already in pool: {len(srpms) - len(missing)}  "
          f"MISSING: {len(missing)}")
    print(f"[inventory] outputs in {OUT_DIR}/ "
          "(image-rpms.tsv / image-rpms-missing.txt / image-rpms-unresolved.tsv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
