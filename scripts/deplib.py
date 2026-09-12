"""Language-level dependency resolution for the casket pipeline.

A collected github archive holds a component's OWN code. Whether its
dependencies come with it is a per-language accident:

  Go     usually vendored in-tree (145/150 of Phase A) -- but not always
  Rust   never: Cargo.lock only
  Node   never: node_modules is not committed
  Python never: requirements/poetry.lock only

So a CVE in a dependency (rustls, golang.org/x/net, lodash, urllib3) cannot be
traced offline for the trees that lack an in-tree copy. These resolvers turn a
lockfile into concrete download URLs so scripts/collect-deps.sh can fetch them.

Every resolver is pure (no network, no fs writes outside the tree it is given)
and returns Dep rows, so tests/test_deps.py can guard them in CI.

Dep = (ecosystem, name, version, url, dest, strip)
  url    where to fetch from; "pypi-api" means the fetcher must ask the JSON API
  dest   path under deps/ to extract into (also the shared store key)
  strip  how to unwrap the archive: "1" = drop one leading dir,
         "go" = drop the leading "<module>@<version>/", "0" = as-is
"""
import fnmatch
import json
import os
import re

CRATES_IO = "https://static.crates.io/crates/{n}/{n}-{v}.crate"
GOPROXY = os.environ.get("GOPROXY_BASE", "https://proxy.golang.org")
GITHUB_PREFIX = "https://github.com/"


def _dep(eco, name, version, url, dest, strip):
    return (eco, name, version, url, dest, strip)


# --------------------------------------------------------------------- rust
def _field(block, key):
    m = re.search(r'^%s = "(.*)"$' % key, block, re.M)
    return m.group(1) if m else ""


def resolve_cargo_lock(text):
    """Cargo.lock -> crates.io .crate archives (+ github archives for git deps).

    Several crates commonly come from ONE git repo+rev (guest-components alone
    provides 7 of trustee's), so git deps key on <repo>-g<sha12>; the caller
    dedups on dest and can symlink the crate names to it.
    """
    out = []
    for block in text.split("\n\n"):
        if not block.lstrip().startswith("[[package]]"):
            continue
        name, ver, src = (_field(block, "name"), _field(block, "version"),
                          _field(block, "source"))
        if not name or not ver or not src:
            continue  # no source = path dep, already inside the tree we fetched
        if src.startswith("registry+"):
            out.append(_dep("crates", name, ver,
                            CRATES_IO.format(n=name, v=ver),
                            f"crates/{name}-{ver}", "1"))
        elif src.startswith("git+"):
            loc = src[4:]
            sha = loc.split("#", 1)[1] if "#" in loc else ""
            repo = loc.split("#", 1)[0].split("?", 1)[0].rstrip("/")
            if repo.endswith(".git"):
                repo = repo[:-4]
            if not repo.startswith(GITHUB_PREFIX) or not sha:
                out.append(_dep("crates", name, ver, repo, "", "0"))
                continue
            reponame = repo.split("/")[-1]
            out.append(_dep("crates", name, ver,
                            f"{repo}/archive/{sha}.tar.gz",
                            f"crates/{reponame}-g{sha[:12]}", "1"))
        else:
            out.append(_dep("crates", name, ver, src, "", "0"))
    return out


# ----------------------------------------------------------------------- go
def go_escape(s):
    """Module proxy path encoding: uppercase -> '!' + lowercase.

    Without this, github.com/Azure/... 404s on proxy.golang.org.
    """
    return "".join("!" + c.lower() if c.isupper() else c for c in s)


def go_version_key(ver):
    """Order Go versions well enough to pick the highest of a module's candidates.

    Handles vX.Y.Z, +incompatible, pre-releases and pseudo-versions
    (v0.0.0-20240719175910-8a7402abbf56, where the timestamp is the real order).
    """
    v = ver.lstrip("v").replace("+incompatible", "")
    base, _, suffix = v.partition("-")
    nums = []
    for part in base.split(".")[:3]:
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    # a release outranks any pre-release/pseudo-version of the same base
    return (nums[0], nums[1], nums[2], 1 if not suffix else 0, suffix)


def resolve_go_sum(text):
    """go.sum -> proxy.golang.org module zips, one version per module path.

    go.sum has two rows per module (the zip hash and a "<ver>/go.mod" row); only
    the first is a real archive. It also carries every version the module graph
    ever mentions, not the one that gets compiled: Go's minimal version
    selection builds exactly ONE version per module path, the highest required.
    Fetching them all cost 1.9 GB of azure-sdk-for-go alone (v46/v57/v67/v68) in
    a single operator casket, for three versions no binary contains -- so keep
    the max and let the rest go.
    """
    best = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        mod, ver = parts[0], parts[1]
        if ver.endswith("/go.mod"):
            continue
        cur = best.get(mod)
        if cur is None or go_version_key(ver) > go_version_key(cur):
            best[mod] = ver
    return [_dep("go", mod, ver,
                 f"{GOPROXY}/{go_escape(mod)}/@v/{go_escape(ver)}.zip",
                 f"go/{mod}@{ver}", "go")
            for mod, ver in sorted(best.items())]


# --------------------------------------------------------------------- node
def resolve_npm_lock(text):
    """package-lock.json (v1 dependencies / v2+ packages) -> registry tarballs.

    The lock already carries the exact `resolved` URL, so no registry query is
    needed. Entries without one (link:/workspace:/file: deps) are in-tree.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    seen = set()

    def add(name, meta):
        if not isinstance(meta, dict):
            return
        ver, url = meta.get("version", ""), meta.get("resolved", "")
        # `"resolved": false` is legal in lockfile v1 (bundled/linked deps) and
        # `version` can be a git URL or a bool there too -- every community
        # operator casket crashed on the bool before this guard.
        if not isinstance(url, str) or not isinstance(ver, str) or not isinstance(name, str):
            return
        if not name or not ver or not url.startswith("http"):
            return
        key = (name, ver)
        if key in seen:
            return
        seen.add(key)
        out.append(_dep("npm", name, ver, url, f"npm/{name}@{ver}", "1"))

    for path, meta in (data.get("packages") or {}).items():
        if not path:
            continue  # "" is the root project itself
        # "node_modules/a/node_modules/b" -> "b"; scoped names keep their @scope
        name = path.split("node_modules/")[-1]
        # a non-dict value is legal-ish in the wild and add() rejects it, but
        # only if we get there -- meta.get() on a str/bool raises first.
        add(meta.get("name") or name if isinstance(meta, dict) else name, meta)

    def walk_v1(deps):
        for name, meta in (deps or {}).items():
            add(name, meta)
            if isinstance(meta, dict):
                walk_v1(meta.get("dependencies"))

    walk_v1(data.get("dependencies"))
    return out


def npm_tarball_url(name, version):
    """Registry layout: .../<name>/-/<basename>-<version>.tgz (scope dropped)."""
    base = name.split("/")[-1]
    return f"https://registry.npmjs.org/{name}/-/{base}-{version}.tgz"


_YARN_RESOLUTION = re.compile(r'^\s+resolution: "(.+)@npm:([^"]+)"', re.M)
_YARN_RESOLVED = re.compile(r'^\s+resolved "([^"#]+)', re.M)
_YARN_V1_ENTRY = re.compile(r'^(?!\s)(.+?):\n(?:\s+.*\n)*?\s+version "([^"]+)"', re.M)


def resolve_yarn_lock(text):
    """yarn.lock -> registry tarballs, for both lock dialects.

    Berry (v2+, `resolution: "pkg@npm:1.2.3"`) carries no URL at all, so it is
    rebuilt from the registry layout; classic v1 has `resolved "<url>#<hash>"`.
    Non-registry resolutions (workspace:/patch:/link:/file:) are in-tree.
    """
    out = []
    seen = set()
    for name, version in _YARN_RESOLUTION.findall(text):
        if (name, version) in seen:
            continue
        seen.add((name, version))
        out.append(_dep("npm", name, version, npm_tarball_url(name, version),
                        f"npm/{name}@{version}", "1"))
    if out:
        return out
    # classic v1: take each entry's `resolved` URL, name from the entry header
    for block in re.split(r"\n(?=\S)", text):
        m = _YARN_RESOLVED.search(block)
        v = re.search(r'^\s+version "([^"]+)"', block, re.M)
        if not m or not v:
            continue
        header = block.split(":\n", 1)[0].strip().strip('"')
        spec = header.split(",")[0].strip().strip('"')
        name = spec[:spec.rindex("@")] if "@" in spec[1:] else spec
        version = v.group(1)
        if (name, version) in seen:
            continue
        seen.add((name, version))
        out.append(_dep("npm", name, version, m.group(1),
                        f"npm/{name}@{version}", "1"))
    return out


# ------------------------------------------------------------------- python
_REQ_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9][A-Za-z0-9.*+!-]*)")


def resolve_python_requirements(text):
    """requirements.txt -> PyPI, but only for `==` pins.

    A `>=` or unpinned line has no single correct answer offline; those are
    reported as uncovered rather than guessed at.
    """
    out = []
    seen = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        m = _REQ_PIN.match(line)
        if not m:
            continue
        name, ver = m.group(1), m.group(2)
        if (name.lower(), ver) in seen:
            continue
        seen.add((name.lower(), ver))
        out.append(_dep("pypi", name, ver, "pypi-api", f"pypi/{name}-{ver}", "1"))
    return out


def resolve_poetry_lock(text):
    """poetry.lock -> PyPI (exact versions, so every entry resolves)."""
    out = []
    seen = set()
    for block in text.split("[[package]]"):
        name = re.search(r'^name = "(.*)"$', block, re.M)
        ver = re.search(r'^version = "(.*)"$', block, re.M)
        if not name or not ver:
            continue
        key = (name.group(1).lower(), ver.group(1))
        if key in seen:
            continue
        seen.add(key)
        out.append(_dep("pypi", name.group(1), ver.group(1), "pypi-api",
                        f"pypi/{name.group(1)}-{ver.group(1)}", "1"))
    return out


# ---------------------------------------------------------------- tree scan
# (fnmatch pattern -> resolver). Patterns, not fixed names, because real repos
# do not use the textbook filename: openshift/ironic pins in
# `python-requirements.okd`, agent-installer-ui locks with `yarn.lock`.
MANIFESTS = [
    ("Cargo.lock", resolve_cargo_lock),
    ("go.sum", resolve_go_sum),
    ("package-lock.json", resolve_npm_lock),
    ("npm-shrinkwrap.json", resolve_npm_lock),
    ("yarn.lock", resolve_yarn_lock),
    ("poetry.lock", resolve_poetry_lock),
    ("*requirements*.txt", resolve_python_requirements),
    ("*requirements*", resolve_python_requirements),
]

# Go and Rust ship their deps in-tree when vendored; nothing to fetch then.
VENDORED_MARKER = {"go.sum": "vendor", "Cargo.lock": "vendor"}


def scan_tree(root, max_depth=2):
    """Walk one collected source tree; yield (manifest_relpath, item) rows.

    item is one of:
      Dep tuple      a dependency to fetch
      None           manifest found, but nothing fetchable in it (unpinned)
      Exception      the resolver blew up on this manifest

    A resolver crash is yielded, never raised: one malformed lockfile used to
    abort the whole casket (a `"resolved": false` in one community operator's
    package-lock took down seven caskets in the 2026-07-26 fleet run). One bad
    manifest may cost its own dependencies -- it must not cost the other 40,000.

    Depth-limited because a big repo can carry hundreds of nested test
    manifests, and those are not what ships in the product image.
    """
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames
                       if d not in ("vendor", "node_modules", ".git", "testdata")]
        done = set()
        for pattern, resolver in MANIFESTS:
            for fn in fnmatch.filter(sorted(filenames), pattern):
                if fn in done:
                    continue          # first pattern that matches a file wins
                done.add(fn)
                marker = VENDORED_MARKER.get(pattern)
                if marker and os.path.isdir(os.path.join(dirpath, marker)):
                    continue  # deps already in-tree
                path = os.path.join(dirpath, fn)
                if os.path.islink(path) or os.path.getsize(path) > 32 * 1024 * 1024:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                rel = os.path.relpath(path, root)
                try:
                    deps = resolver(text)
                except Exception as exc:      # noqa: BLE001 - reported, not raised
                    yield rel, exc
                    continue
                if not deps:
                    # A manifest we could not turn into a single download (an
                    # unpinned requirements list, a lock we can't parse) must be
                    # NAMED, not silently absent from the report.
                    yield rel, None
                    continue
                for dep in deps:
                    yield rel, dep
