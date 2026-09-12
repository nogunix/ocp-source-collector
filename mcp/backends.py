"""Backends for casket-mcp — filesystem navigation + ripgrep search over the
mounted casket sources at /srv/sources-*.

Stage 1: FS + ripgrep only (no OpenGrok). All operations are read-only and
confined to ROOTS via path validation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Mount roots we are willing to read. realpath of any requested path must live
# under one of these prefixes.
ROOT_PREFIX = "/srv/sources-"
SRV = "/srv"

# OpenGrok (stage 2). Indexes the /srv/opengrok-src symlink tree — all phases
# (a / a-rpm / b / b-certified / b-community / b-operand) since 2026-06-08.
OPENGROK_URL = os.environ.get("OPENGROK_URL", "http://localhost:8080").rstrip("/")
OPENGROK_SRC = os.environ.get("OPENGROK_SRC", "/srv/opengrok-src")


# ------------------------------------------------------------------ discovery
@dataclass(frozen=True)
class Mount:
    path: str          # /srv/sources-ocp4.18.46
    name: str          # sources-ocp4.18.46
    suffix: str        # ocp4.18.46 | ocp4.18-operators | ocp4.18-certified-operators | layered-ocp4.18 | ocp-srpms
    phase: str         # a | a-rpm | b | b-certified | b-community | b-operand | other
    version: str       # 4.18.46 | 4.18 | "" (srpms)


_PATCH_RE = re.compile(r"^ocp(\d+\.\d+\.\d+)$")
_CATALOG_RE = re.compile(r"^ocp(\d+\.\d+)-(certified|community)-operators$")
_OPER_RE = re.compile(r"^ocp(\d+\.\d+)-operators$")
_LAYERED_RE = re.compile(r"^layered-ocp(\d+\.\d+)$")


def _classify(suffix: str) -> tuple[str, str]:
    # Phase ids follow the repo's 2026-07-11 naming (README「現状」):
    #   a = payload component sources, a-rpm = rhel-coreos SRPMs,
    #   b = redhat-operators, b-operand = layered products,
    #   b-certified / b-community = the extra catalog caskets (2026-07-11).
    if m := _PATCH_RE.match(suffix):
        return "a", m.group(1)
    if m := _CATALOG_RE.match(suffix):
        return f"b-{m.group(2)}", m.group(1)
    if m := _OPER_RE.match(suffix):
        return "b", m.group(1)
    if m := _LAYERED_RE.match(suffix):
        return "b-operand", m.group(1)
    if suffix == "ocp-srpms":
        return "a-rpm", ""
    return "other", ""


def list_mounts() -> list[Mount]:
    out: list[Mount] = []
    try:
        names = sorted(os.listdir(SRV))
    except OSError:
        return out
    for name in names:
        if not name.startswith("sources-"):
            continue
        path = os.path.join(SRV, name)
        if not os.path.isdir(path):
            continue
        suffix = name[len("sources-"):]
        phase, version = _classify(suffix)
        out.append(Mount(path=path, name=name, suffix=suffix, phase=phase, version=version))
    return out


def _mounts_for_version(version: str, phase: str | None = None) -> list[Mount]:
    res = []
    for m in list_mounts():
        if phase and m.phase != phase:
            continue
        if version and not (m.version == version or m.version.startswith(version + ".")
                            or version.startswith(m.version + ".") or m.version == version):
            # match 4.18 against 4.18.41 (patch) and against 4.18 (minor)
            if not (m.version == version or m.version.split(".")[:2] == version.split(".")[:2]):
                continue
        res.append(m)
    return res


# --------------------------------------------------------------- path safety
def safe_path(path: str) -> str:
    """Resolve and confine a path to the casket mount roots. Raises ValueError."""
    rp = os.path.realpath(path)
    if not (rp == SRV or rp.startswith(ROOT_PREFIX) or rp.startswith(SRV + "/sources-")):
        raise ValueError(f"path outside casket roots: {path}")
    return rp


# ------------------------------------------------------------------ resolve
def _index_units(mount: Mount):
    """Yield (unit_label, unit_dir) holding by-repo/by-component/git/INDEX.tsv.
    Single layout (a/b/catalogs): the mount root. b-operand: per-product subdirs."""
    if os.path.isfile(os.path.join(mount.path, "git", "INDEX.tsv")):
        yield "", mount.path
        return
    try:
        for sub in sorted(os.listdir(mount.path)):
            d = os.path.join(mount.path, sub)
            if os.path.isfile(os.path.join(d, "git", "INDEX.tsv")):
                yield sub, d
    except OSError:
        pass


_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _sub_repo_url(slug: str) -> str:
    """SUBMODULES.tsv records github repos as `owner/name`; make it a URL so a
    submodule hit reads like an INDEX.tsv row. Non-github refs are kept raw."""
    return f"https://github.com/{slug}" if _SLUG_RE.match(slug) else slug


def _submodule_rows(unit: str) -> list[dict]:
    """Parse <unit>/meta/SUBMODULES.tsv — the trees filled into what a codeload
    archive leaves as empty gitlink dirs.

    These are invisible to by-component/, by-repo/ and INDEX.tsv, which name
    only the top-level tree. That tree is frequently a wrapper repo
    (`<name>-release`) holding Containerfiles and nothing else, so a component
    resolved from the index alone can look absent when its code is one level
    down. Rows whose directory is not on the mount (older casket, failed fetch)
    are dropped, and a submodule reachable under several sibling wrappers is
    reported once."""
    raw: list[dict] = []
    try:
        f = open(os.path.join(unit, "meta", "SUBMODULES.tsv"))
    except OSError:
        return raw
    with f:
        next(f, None)  # header: component | path | repo | ref | exact | status
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 5:
                continue
            parent, sub, slug, ref, exact = cols[0], cols[1], cols[2], cols[3], cols[4]
            tree = os.path.join(unit, "git", parent, sub)
            if not (parent and sub and os.path.isdir(tree)):
                continue
            raw.append({
                "parent": parent, "sub": sub, "slug": slug,
                "repo": _sub_repo_url(slug), "ref": ref,
                "ref_exact": exact == "1", "path": tree,
            })
    # Exact-ref trees first: a wrapper pinning the branch head (exact=0) is an
    # approximation of what was built, and must not outrank the pinned commit.
    raw.sort(key=lambda r: (not r["ref_exact"], r["parent"], r["sub"]))
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for r in raw:
        key = (r["slug"].lower(), r["ref"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    return rows


def _sub_matches(row: dict, query: str) -> bool:
    """Substring match on the submodule's repo (slug, basename) or its path,
    in both directions like resolve_repo's by-repo matching."""
    q = query.lower().rstrip("/").removesuffix(".git")
    if not q:
        return False
    slug = row["slug"].lower()
    base = slug.rsplit("/", 1)[-1]
    return q in slug or base in q or q in row["sub"].lower()


def resolve_repo(repo: str, version: str) -> list[dict]:
    """Find source tree(s) for an upstream repo via the by-repo/ index.
    by-repo entries are repo basenames (e.g. "kubevirt"), so a query like
    "kubevirt/kubevirt" or a full URL is normalized to its basename first."""
    q = repo.lower().rstrip("/")
    qbase = q.rsplit("/", 1)[-1].removesuffix(".git")
    hits: list[dict] = []
    for m in _mounts_for_version(version):
        for label, unit in _index_units(m):
            byrepo = os.path.join(unit, "by-repo")
            if not os.path.isdir(byrepo):
                continue
            for entry in os.listdir(byrepo):
                el = entry.lower()
                if qbase and (qbase in el or el in qbase):
                    link = os.path.join(byrepo, entry)
                    hits.append({
                        "mount": m.name, "phase": m.phase, "product": label or None,
                        "repo_alias": entry, "path": os.path.realpath(link),
                        "exact": el == qbase,
                    })
            for row in _submodule_rows(unit):
                if _sub_matches(row, qbase):
                    hits.append({
                        "mount": m.name, "phase": m.phase, "product": label or None,
                        "repo_alias": row["slug"], "path": row["path"],
                        "exact": row["slug"].lower().rsplit("/", 1)[-1] == qbase,
                        "submodule_of": row["parent"], "ref": row["ref"],
                        "ref_exact": row["ref_exact"],
                    })
    hits.sort(key=lambda h: (not h["exact"], h["mount"], h["repo_alias"]))
    return hits


def resolve_component(name: str, version: str) -> list[dict]:
    """Find source tree(s) for a component via the by-component/ index."""
    hits: list[dict] = []
    for m in _mounts_for_version(version):
        for label, unit in _index_units(m):
            bycomp = os.path.join(unit, "by-component")
            if not os.path.isdir(bycomp):
                continue
            for entry in os.listdir(bycomp):
                if name.lower() in entry.lower():
                    link = os.path.join(bycomp, entry)
                    hits.append({
                        "mount": m.name, "phase": m.phase, "product": label or None,
                        "component": entry, "path": os.path.realpath(link),
                    })
            for row in _submodule_rows(unit):
                if _sub_matches(row, name):
                    hits.append({
                        "mount": m.name, "phase": m.phase, "product": label or None,
                        "component": row["slug"].rsplit("/", 1)[-1],
                        "path": row["path"], "submodule_of": row["parent"],
                        "repo": row["repo"], "ref": row["ref"],
                        "ref_exact": row["ref_exact"],
                    })
    return hits


def list_components(version: str, phase: str | None = None) -> list[dict]:
    out: list[dict] = []
    for m in _mounts_for_version(version, phase):
        for label, unit in _index_units(m):
            idx = os.path.join(unit, "git", "INDEX.tsv")
            try:
                with open(idx) as f:
                    next(f, None)  # header
                    for line in f:
                        cols = line.rstrip("\n").split("\t")
                        if len(cols) >= 5:
                            out.append({
                                "mount": m.name, "phase": m.phase, "product": label or None,
                                "dir": cols[0], "repo": cols[1], "version": cols[3],
                                "components": cols[4].split(",") if cols[4] else [],
                            })
            except OSError:
                pass
            for row in _submodule_rows(unit):
                out.append({
                    "mount": m.name, "phase": m.phase, "product": label or None,
                    "dir": f"{row['parent']}/{row['sub']}", "repo": row["repo"],
                    "version": "", "components": [row["slug"].rsplit("/", 1)[-1]],
                    "submodule_of": row["parent"], "ref": row["ref"],
                    "ref_exact": row["ref_exact"],
                })
    return out


# ------------------------------------------------------------------ read
def read_file(path: str, start: int | None = None, end: int | None = None,
              max_bytes: int = 200_000) -> dict:
    rp = safe_path(path)
    if not os.path.isfile(rp):
        raise ValueError(f"not a file: {path}")
    with open(rp, "r", errors="replace") as f:
        lines = f.readlines()
    n = len(lines)
    s = (start - 1) if start else 0
    e = end if end else n
    s = max(0, s); e = min(n, e)
    chunk = lines[s:e]
    text = "".join(chunk)
    truncated = False
    if len(text) > max_bytes:
        text = text[:max_bytes]
        truncated = True
    return {"path": rp, "total_lines": n, "start": s + 1, "end": e,
            "truncated": truncated, "content": text}


def diff_file(path_a: str, path_b: str, context: int = 3,
              max_lines: int = 4000) -> dict:
    """Unified diff between two source files (e.g. the same file across two OCP
    versions). Both paths confined to /srv/sources-*."""
    import difflib
    ra, rb = safe_path(path_a), safe_path(path_b)
    for p in (ra, rb):
        if not os.path.isfile(p):
            raise ValueError(f"not a file: {p}")
    with open(ra, errors="replace") as f:
        a = f.readlines()
    with open(rb, errors="replace") as f:
        b = f.readlines()
    diff = list(difflib.unified_diff(a, b, fromfile=ra, tofile=rb, n=context))
    truncated = len(diff) > max_lines
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    return {"a": ra, "b": rb, "added": added, "removed": removed,
            "identical": not diff, "truncated": truncated,
            "diff": "".join(diff[:max_lines])}


def list_dir(path: str) -> dict:
    rp = safe_path(path)
    if not os.path.isdir(rp):
        raise ValueError(f"not a directory: {path}")
    entries = []
    for name in sorted(os.listdir(rp)):
        full = os.path.join(rp, name)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(full) else
                    ("link" if os.path.islink(full) else "file"),
        })
    return {"path": rp, "entries": entries}


# ------------------------------------------------------------------ ripgrep
def _rg(args: list[str], timeout: int = 60) -> list[dict]:
    """Run rg --json and parse match events into [{path,line,text}]."""
    try:
        proc = subprocess.run(
            ["rg", "--json", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [{"error": "ripgrep timeout"}]
    results: list[dict] = []
    for raw in proc.stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") != "match":
            continue
        d = ev["data"]
        results.append({
            "path": d["path"].get("text", ""),
            "line": d["line_number"],
            "text": d["lines"].get("text", "").rstrip("\n"),
        })
    return results


def grep(pattern: str, path: str, glob: str | None = None,
         max_results: int = 100, ignore_case: bool = False) -> dict:
    rp = safe_path(path)
    args = ["-n", f"--max-count={max_results}"]
    if ignore_case:
        args.append("-i")
    if glob:
        args += ["-g", glob]
    args += ["-e", pattern, rp]
    res = _rg(args)
    return {"backend": "ripgrep", "pattern": pattern, "root": rp,
            "count": len(res), "results": res[:max_results]}


def _rg_search_text(query, version, path, max_results, ignore_case) -> dict:
    roots: list[str] = []
    if path:
        roots = [safe_path(path)]
    elif version:
        roots = [m.path for m in _mounts_for_version(version)]
    else:
        roots = [m.path for m in list_mounts()]
    if not roots:
        return {"backend": "ripgrep", "query": query, "count": 0, "results": [],
                "note": "no matching mounts"}
    args = ["-n", f"--max-count={max_results}"]
    if ignore_case:
        args.append("-i")
    args += ["-e", query, *roots]
    res = _rg(args)
    return {"backend": "ripgrep", "query": query, "roots": roots,
            "count": len(res), "results": res[:max_results]}


_RG_SLOW_NOTE = ("Whole-mount ripgrep is impractical here (~2 min: squashfs-xz "
                 "decompression). Pass a `path` from resolve_repo/resolve_component "
                 "to grep a scoped tree (~seconds), or start OpenGrok and use "
                 "engine='opengrok' for fast broad search.")


def search_text(query: str, version: str | None = None, path: str | None = None,
                max_results: int = 100, ignore_case: bool = True,
                engine: str = "auto", project: str | None = None) -> dict:
    """Full-text search. engine: "auto" (OpenGrok if up, else require a path),
    "opengrok", or "ripgrep". An explicit `path` always uses ripgrep (fast).
    OpenGrok covers every phase (incl. b-operand and the catalog caskets).
    Broad ripgrep over whole mounts is refused (too slow) — scope with a path
    or use OpenGrok."""
    if path:  # scoped ripgrep — fast, all phases
        return _rg_search_text(query, version, path, max_results, ignore_case)
    if engine in ("auto", "opengrok") and opengrok_up():
        og = opengrok_search("full", query, project, max_results)
        if "error" not in og:
            return og
        if engine == "opengrok":
            return og  # surface the error rather than a 2-min scan
    # No path, OpenGrok unavailable/not requested -> don't scan whole mounts.
    return {"backend": "none", "query": query, "count": 0, "results": [],
            "error": _RG_SLOW_NOTE}


# ------------------------------------------------------------------ opengrok
_TAG_RE = re.compile(r"<[^>]+>")


def opengrok_up(timeout: float = 3.0) -> bool:
    """Reachability check. The /api/v1/projects endpoint requires auth (401) but
    /api/v1/search does not, so probe search. An HTTP error response still means
    the server is up; only connection/timeout errors mean down."""
    try:
        urllib.request.urlopen(
            OPENGROK_URL + "/api/v1/search?" + urllib.parse.urlencode(
                {"path": "/__ping__", "maxresults": 1}),
            timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # server responded (e.g. 401/400) → it's up
    except Exception:
        return False


def opengrok_projects(timeout: float = 5.0) -> list[str]:
    try:
        with urllib.request.urlopen(OPENGROK_URL + "/api/v1/projects", timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return []
    # API may return a list of names or a list of project objects
    out = []
    for p in data if isinstance(data, list) else []:
        out.append(p if isinstance(p, str) else p.get("name", ""))
    return [p for p in out if p]


def opengrok_search(mode: str, query: str, projects: str | None = None,
                    max_results: int = 50, timeout: float = 20.0) -> dict:
    """mode: def | symbol | full | path. Returns {backend, count, results:[{path,
    line, text, opengrok_path}]} or {error}. Result paths are resolved through
    the /srv/opengrok-src symlink tree back to the real casket path."""
    param = {"def": "def", "symbol": "symbol", "full": "full", "path": "path"}.get(mode)
    if not param:
        return {"error": f"bad mode: {mode}"}
    qs = {param: query, "maxresults": max_results}
    if projects:
        qs["projects"] = projects
    url = OPENGROK_URL + "/api/v1/search?" + urllib.parse.urlencode(qs)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except Exception as e:
        return {"error": f"opengrok unreachable: {e}", "backend": "opengrok"}
    results: list[dict] = []
    for fpath, hits in (data.get("results") or {}).items():
        real = os.path.realpath(OPENGROK_SRC + fpath)
        for h in hits:
            ln = h.get("lineNumber") or h.get("line_number") or 0
            try:
                ln = int(ln)
            except (TypeError, ValueError):
                ln = 0
            text = _TAG_RE.sub("", h.get("line", "")).strip()
            results.append({"path": real, "opengrok_path": fpath, "line": ln, "text": text})
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break
    return {"backend": "opengrok", "mode": mode, "query": query,
            "count": len(results), "results": results}


def _rg_def_fallback(name: str, path: str) -> dict:
    """Approximate 'definition' search with ripgrep within a scoped path (only —
    whole-mount scan is too slow on squashfs-xz). Declaration keywords only."""
    rp = safe_path(path)
    pat = rf"\b(type|func|class|def|interface|struct|const|var|enum)\s+{re.escape(name)}\b"
    res = _rg(["-n", "--max-count=50", "-e", pat, rp], timeout=60)
    return {"backend": "ripgrep(opengrok down — approximate def, scoped)",
            "query": name, "root": rp, "count": len(res), "results": res[:50]}


def search_symbol(name: str, project: str | None = None,
                  path: str | None = None) -> dict:
    """Definition search (OpenGrok 'def') across all phases (b-operand =
    projects `layered-<minor>`; catalogs = `certified-`/`community-<minor>`).
    When OpenGrok is down, falls back to a scoped ripgrep approximation only
    if `path` is given (resolve_repo first) — it will not scan whole mounts."""
    if opengrok_up():
        og = opengrok_search("def", name, project)
        if "error" not in og:
            return og
    if path:
        return _rg_def_fallback(name, path)
    return {"backend": "none", "query": name, "count": 0, "results": [],
            "error": ("OpenGrok is down (needed for broad symbol search). Start "
                      "../opengrok/scripts/run-opengrok.sh, or pass `path` (from "
                      "resolve_repo) for a scoped ripgrep definition approximation.")}


def search_refs(name: str, project: str | None = None) -> dict:
    """Reference / cross-reference search (OpenGrok 'symbol'). Requires OpenGrok
    (ripgrep cannot distinguish references) — returns an error note if down."""
    if not opengrok_up():
        return {"backend": "opengrok", "error": "OpenGrok is not running; "
                "xref search needs it. Start ../opengrok/scripts/run-opengrok.sh, "
                "or use grep/search_text for a plain-text approximation.",
                "count": 0, "results": []}
    return opengrok_search("symbol", name, project)


# ------------------------------------------------------ dependency lockfiles
_LOCK_SKIP_DIRS = {"vendor", "node_modules", ".git", "testdata", "test",
                   "tests", "examples", "patches", "third_party"}


def _find_lockfiles(root: str, filenames: set[str],
                    max_depth: int = 4, limit: int = 30) -> list[str]:
    """Locate lockfiles under root, skipping vendored/test trees. Depth-capped:
    workspace locks live at or near the repo root."""
    found: list[str] = []
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, files in os.walk(root):
        if dirpath.count("/") - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _LOCK_SKIP_DIRS]
        for f in files:
            if f in filenames:
                found.append(os.path.join(dirpath, f))
                if len(found) >= limit:
                    return found
    return found


def _cargo_lock_matches(lock_path: str, name: str) -> list[dict]:
    import tomllib
    try:
        with open(lock_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:  # malformed lock: report, don't crash the tool
        return [{"lockfile": lock_path, "error": f"parse failed: {e}"}]
    pkgs = data.get("package", [])
    # reverse-dependency map (dependency entries may be "name" or "name x.y.z")
    rev: dict[str, list[str]] = {}
    for p in pkgs:
        for dep in p.get("dependencies", []) or []:
            rev.setdefault(dep.split(" ")[0], []).append(
                f'{p.get("name")}-{p.get("version")}')
    out = []
    exact = [p for p in pkgs if p.get("name") == name]
    for p in (exact or [p for p in pkgs if name in (p.get("name") or "")]):
        out.append({
            "kind": "cargo", "lockfile": lock_path,
            "name": p.get("name"), "version": p.get("version"),
            "source": p.get("source") or "workspace-local",
            "checksum": p.get("checksum"),
            "dependents": sorted(rev.get(p.get("name"), []))[:15],
        })
    return out


_GO_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9._~/\-]+)\s+(v[0-9][^\s]*)\s*(//\s*indirect)?\s*$")
_GO_REPL_RE = re.compile(
    r"^\s*([A-Za-z0-9._~/\-]+)\s*(?:v[^\s]+)?\s*=>\s*(\S+)\s*(v[^\s]+)?")


def _go_mod_matches(mod_path: str, name: str) -> list[dict]:
    out = []
    try:
        text = open(mod_path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        return [{"lockfile": mod_path, "error": str(e)}]
    module = ""
    replaces: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("module "):
            module = line.split(None, 1)[1].strip()
        if m := _GO_REPL_RE.match(line):
            replaces[m.group(1)] = f"{m.group(2)} {m.group(3) or ''}".strip()
    for line in text.splitlines():
        if m := _GO_REQ_RE.match(line):
            path_, ver, indirect = m.group(1), m.group(2), bool(m.group(3))
            if name in path_:
                out.append({
                    "kind": "go", "lockfile": mod_path, "module": module,
                    "name": path_, "version": ver, "indirect": indirect,
                    "replaced_by": replaces.get(path_),
                })
    return out


def resolve_dependency(repo_path: str, name: str, kind: str = "auto",
                       max_results: int = 20) -> dict:
    """Look a dependency up in the lockfiles of a casket source tree.
    kind: cargo | go | auto. Cargo.lock gives exact locked versions +
    reverse-dependents; go.mod gives required versions (+ replace directives).
    NOTE: Cargo.lock does not record which cargo *features* were enabled at
    build time — grep the spec/build scripts for that."""
    root = safe_path(repo_path)
    if not os.path.isdir(root):
        return {"error": f"not a directory: {repo_path}"}
    want_cargo = kind in ("auto", "cargo")
    want_go = kind in ("auto", "go")
    matches: list[dict] = []
    lockfiles: list[str] = []
    if want_cargo:
        for lf in _find_lockfiles(root, {"Cargo.lock"}):
            lockfiles.append(lf)
            matches += _cargo_lock_matches(lf, name)
    if want_go:
        for lf in _find_lockfiles(root, {"go.mod"}):
            lockfiles.append(lf)
            matches += _go_mod_matches(lf, name)
    return {
        "query": name, "root": root, "kind": kind,
        "lockfiles_scanned": len(lockfiles),
        "count": len(matches), "matches": matches[:max_results],
        **({"note": "no lockfiles found under root (depth cap 4; vendor/ "
                    "and test trees skipped)"} if not lockfiles else {}),
    }


# ----------------------------------------------------------- permalink
def _parse_index(idx_path: str) -> list[tuple[str, str, str]]:
    """Read INDEX.tsv → [(dir, repo_url, ref), ...]."""
    rows: list[tuple[str, str, str]] = []
    try:
        with open(idx_path) as f:
            next(f, None)  # header
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 3:
                    rows.append((cols[0], cols[1], cols[2]))
    except OSError:
        pass
    return rows


def _parse_submodules(tsv_path: str) -> list[dict]:
    """Read meta/SUBMODULES.tsv → [{component, path, repo, ref, exact}, ...]."""
    rows: list[dict] = []
    try:
        with open(tsv_path) as f:
            for line in f:
                if line.startswith("#"):
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 5:
                    rows.append({
                        "component": cols[0], "path": cols[1],
                        "repo": cols[2], "ref": cols[3],
                        "exact": cols[4] == "1",
                    })
    except OSError:
        pass
    return rows


def _github_url(repo_url: str) -> str | None:
    """Extract a GitHub base URL from a repo URL, or None if not GitHub."""
    if not repo_url:
        return None
    u = repo_url.rstrip("/").removesuffix(".git")
    if "github.com" not in u:
        return None
    return u


def permalink(path: str, line: int = 0) -> dict:
    """Build a GitHub permalink for a file in a casket source tree.

    Resolves through INDEX.tsv (direct tree) and SUBMODULES.tsv (submodule
    trees) to construct a permalink with the exact commit SHA. The caller
    never needs to grep INDEX.tsv or handle submodule path stripping."""
    rp = safe_path(path)
    if not os.path.exists(rp):
        return {"error": f"path does not exist: {path}"}

    # 1. Find which mount this path belongs to
    mount: Mount | None = None
    for m in list_mounts():
        if rp.startswith(m.path + "/") or rp == m.path:
            mount = m
            break
    if not mount:
        return {"error": f"path not under any mounted casket: {path}"}

    rel_to_mount = rp[len(mount.path):].lstrip("/")

    # 2. Find the unit (for b-operand: product subdir)
    unit_dir: str | None = None
    unit_label = ""
    for label, udir in _index_units(mount):
        prefix = udir[len(mount.path):].lstrip("/")
        if not prefix or rel_to_mount.startswith(prefix + "/") or rel_to_mount == prefix:
            unit_dir = udir
            unit_label = label
            break
    if not unit_dir:
        return {"error": f"no INDEX.tsv found for mount {mount.name}"}

    # 3. Determine the git dir and relative path within it
    git_root = os.path.join(unit_dir, "git")
    if not rp.startswith(git_root + "/"):
        return {"error": f"path is not under git/ in {unit_dir}"}

    rel_to_git = rp[len(git_root):].lstrip("/")
    # First path component is the source dir name
    parts = rel_to_git.split("/", 1)
    src_dir = parts[0]
    file_in_tree = parts[1] if len(parts) > 1 else ""

    # 4. Look up in INDEX.tsv
    idx_path = os.path.join(git_root, "INDEX.tsv")
    idx_rows = _parse_index(idx_path)
    idx_match = None
    for d, repo_url, ref in idx_rows:
        if d == src_dir:
            idx_match = (d, repo_url, ref)
            break
    if not idx_match:
        return {"error": f"dir '{src_dir}' not found in {idx_path}",
                "path": rp, "mount": mount.name}

    _, repo_url, ref = idx_match

    # 5. Check submodules — is this file inside a filled submodule?
    sub_tsv = os.path.join(unit_dir, "meta", "SUBMODULES.tsv")
    subs = _parse_submodules(sub_tsv)
    sub_match = None
    for s in subs:
        if s["component"] != src_dir:
            continue
        sub_path = s["path"]
        if file_in_tree == sub_path or file_in_tree.startswith(sub_path + "/"):
            sub_match = s
            break

    if sub_match:
        # Use the submodule's repo and ref; strip the submodule path prefix
        slug = sub_match["repo"]  # org/repo format from SUBMODULES.tsv
        sub_ref = sub_match["ref"]
        exact = sub_match["exact"]
        file_in_sub = file_in_tree[len(sub_match["path"]):].lstrip("/")
        gh_base = f"https://github.com/{slug}"
        gh_path = file_in_sub
        result = {
            "url": f"{gh_base}/blob/{sub_ref}/{gh_path}",
            "repo": gh_base, "ref": sub_ref, "exact": exact,
            "source": "SUBMODULES",
            "file": gh_path, "path": rp, "mount": mount.name,
        }
        if not exact:
            result["note"] = ("ref is a branch-head approximation, not the "
                              "commit that was built — treat as approximate")
    else:
        # Direct tree from INDEX.tsv
        gh_base = _github_url(repo_url)
        if not gh_base:
            return {"url": None, "repo": repo_url, "ref": ref,
                    "exact": True, "source": "INDEX",
                    "file": file_in_tree, "path": rp, "mount": mount.name,
                    "note": "repo is not on GitHub — no permalink available"}
        result = {
            "url": f"{gh_base}/blob/{ref}/{file_in_tree}",
            "repo": gh_base, "ref": ref, "exact": True,
            "source": "INDEX",
            "file": file_in_tree, "path": rp, "mount": mount.name,
        }

    if line and result.get("url"):
        result["url"] += f"#L{line}"
        result["line"] = line

    return result


# --------------------------------------------------------- coverage report
# Structural gaps that repeatedly bite source-trace — kept in sync with
# docs/phase-b-uncollected-plan.md / phase-b-operand-plan.md "known limits".
KNOWN_GAPS = {
    "pipelines": "product v1.x ↔ tektoncd v0.x version mapping unsolved (phase b)",
    "compliance": "RH builds ahead of public tags (phase b)",
    "virtio-win": "cnv: no public commit linkage for the container-disk build",
    "clair/quay-builder": "quay: RH-internal builds, no public upstream match",
    "non-github CSV repos": "operators whose CSV repo points at access.redhat.com etc.",
}


def _index_stats(unit_dir: str) -> dict | None:
    idx = os.path.join(unit_dir, "git", "INDEX.tsv")
    try:
        comps, repos, dirs = set(), set(), 0
        with open(idx) as f:
            next(f, None)
            for line in f:
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 5:
                    dirs += 1
                    if cols[1]:
                        repos.add(cols[1])
                    for c in (cols[4] or "").split(","):
                        if c:
                            comps.add(c)
        return {"source_dirs": dirs, "repos": len(repos), "components": len(comps)}
    except OSError:
        return None


def coverage_report(version: str) -> dict:
    """What the mounted caskets cover for an OCP version (minor or patch):
    per-phase component/repo counts, per-product breakdown for b-operand,
    SRPM count for a-rpm, plus the known structural gaps. Lets source-trace
    check coverage BEFORE searching, and route to github-trace early."""
    phases: dict[str, object] = {}
    for m in _mounts_for_version(version):
        if m.phase == "b-operand":
            prods = {}
            for label, unit in _index_units(m):
                prods[label or "_root"] = _index_stats(unit)
            phases["b-operand"] = {"mount": m.name, "products": prods}
        elif m.phase != "other":
            units = list(_index_units(m))
            if units:
                phases[m.phase] = {"mount": m.name, **(_index_stats(units[0][1]) or {})}
    # a-rpm: by-ocp/<patch>/ carries the per-release SRPM view
    srpms_root = "/srv/sources-ocp-srpms/by-ocp"
    if os.path.isdir(srpms_root):
        for pv in sorted(os.listdir(srpms_root)):
            if pv == version or pv.startswith(version.rstrip(".") + "."):
                try:
                    n = len(os.listdir(os.path.join(srpms_root, pv)))
                except OSError:
                    continue
                phases["a-rpm"] = {"mount": "sources-ocp-srpms",
                                   "ocp_version": pv, "srpms": n}
    # expected b-operand products from the repo config (absence = gap)
    expected = []
    cfg = os.path.expanduser("~/casket-work/config/phase-b-operand-products.tsv")
    try:
        with open(cfg) as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    expected.append(line.rstrip("\n").split("\t")[1])
    except OSError:
        pass
    missing = []
    if expected and isinstance(phases.get("b-operand"), dict):
        have = set(phases["b-operand"]["products"])  # type: ignore[index]
        missing = [p for p in expected if p not in have]
    return {
        "version": version,
        "phases": phases or {"note": f"no mounted casket matches {version}"},
        "expected_b_operand_products": expected or None,
        "missing_b_operand_products": missing,
        "known_gaps": KNOWN_GAPS,
    }
