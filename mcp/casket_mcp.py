#!/usr/bin/env python3
"""casket-mcp — MCP server exposing the OpenShift / CNV / RHEL source caskets
mounted at /srv/sources-* for search and reading.

Backends: OpenGrok REST (symbol def / xref / broad full-text, all phases incl.
all phases) + filesystem & scoped ripgrep. See ../docs/casket-mcp-design.md.
Run:  python casket_mcp.py            # stdio (default; for `claude mcp add`)
      python casket_mcp.py http       # streamable-http on 0.0.0.0:8765
"""
from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

import backends as be

mcp = FastMCP("casket")


@mcp.tool()
def list_versions() -> list[dict]:
    """List the OpenShift source caskets currently mounted, with their phase and
    version. Phases (2026-07-11 naming): a = OCP payload component sources,
    a-rpm = rhel-coreos SRPMs, b = redhat-operators, b-certified / b-community =
    the extra catalog caskets, b-operand = layered products
    (cnv/acs/mce/acm/rhoai/odf/quay)."""
    return [
        {"mount": m.name, "phase": m.phase, "version": m.version, "path": m.path}
        for m in be.list_mounts()
    ]


@mcp.tool()
def list_components(version: str, phase: str = "") -> list[dict]:
    """List component source trees for an OCP version (e.g. "4.18" or "4.18.41").
    Optional phase filter: A | B | C | D. Reads each mount's git/INDEX.tsv —
    columns dir | repo | version | components (components share a dedup dir) —
    plus the filled-in submodule trees from meta/SUBMODULES.tsv, which INDEX.tsv
    does not name (those rows carry submodule_of / ref / ref_exact)."""
    return be.list_components(version, phase or None)


@mcp.tool()
def resolve_repo(repo: str, version: str) -> list[dict]:
    """Find the on-disk source tree for an upstream GitHub repo (substring match,
    e.g. "kubevirt", "containerized-data-importer") at an OCP version, via the
    by-repo/ index, plus meta/SUBMODULES.tsv. This defeats the dedup dir-naming
    gotcha where the real repo hides under an unrelated first-seen-component
    name, and the wrapper-repo gotcha where the code sits in a submodule of an
    indexed `<name>-release` tree (hit carries submodule_of, ref, ref_exact —
    ref_exact=false means a branch head, not the commit that was built)."""
    return be.resolve_repo(repo, version)


@mcp.tool()
def resolve_component(name: str, version: str) -> list[dict]:
    """Find the on-disk source tree for a component / image / operator by name
    (substring match) at an OCP version, via the by-component/ index and
    meta/SUBMODULES.tsv. A hit with submodule_of is code that lives one level
    down inside an indexed wrapper tree (e.g. the ZTWIM operator under
    openshift/zero-trust-workload-identity-manager-release); build its permalink
    from that hit's own repo/ref, not from the wrapper's INDEX.tsv row."""
    return be.resolve_component(name, version)


@mcp.tool()
def search_text(query: str, version: str = "", path: str = "",
                max_results: int = 100, ignore_case: bool = True,
                engine: str = "auto", project: str = "") -> dict:
    """Full-text search across casket sources. engine: "auto" (OpenGrok if the
    container is up, else ripgrep), "opengrok", or "ripgrep". An explicit `path`
    always uses ripgrep and is fastest — narrow with a path from resolve_repo.
    OpenGrok covers all phases incl. b-operand (`project` e.g. "ocp-4.18.46",
    "operators-4.18", "layered-4.20"); ripgrep `path` stays available for any tree."""
    return be.search_text(query, version or None, path or None, max_results,
                          ignore_case, engine, project or None)


@mcp.tool()
def search_symbol(name: str, project: str = "", path: str = "") -> dict:
    """Find where a symbol is DEFINED (OpenGrok 'def' — types, funcs, consts).
    Covers all phases incl. b-operand (`project` e.g. "ocp-4.20.27", "operators-4.18",
    "layered-4.20"). When OpenGrok is down, pass `path` (from resolve_repo) for a
    scoped ripgrep approximation — whole-mount scans are refused (too slow)."""
    return be.search_symbol(name, project or None, path or None)


@mcp.tool()
def search_refs(name: str, project: str = "") -> dict:
    """Find REFERENCES to a symbol (OpenGrok cross-reference 'symbol' search).
    Requires the OpenGrok container running — ripgrep cannot distinguish a
    reference from a definition. Returns an error note if OpenGrok is down."""
    return be.search_refs(name, project or None)


@mcp.tool()
def grep(pattern: str, path: str, glob: str = "", max_results: int = 100,
         ignore_case: bool = False) -> dict:
    """Regex search (ripgrep) within a specific path. `glob` filters files
    (e.g. "*.go"). Use after resolve_repo/resolve_component to scope tightly."""
    return be.grep(pattern, path, glob or None, max_results, ignore_case)


@mcp.tool()
def read_file(path: str, start: int = 0, end: int = 0) -> dict:
    """Read a source file (read-only). Optional 1-based line range [start, end].
    Output is capped; request a range for large files. Path must be under
    /srv/sources-*."""
    return be.read_file(path, start or None, end or None)


@mcp.tool()
def diff_file(path_a: str, path_b: str, context: int = 3) -> dict:
    """Unified diff between two source files — e.g. the same file across two OCP
    versions (resolve_repo each version, then diff). Returns added/removed counts
    and the diff text (capped). Both paths must be under /srv/sources-*."""
    return be.diff_file(path_a, path_b, context)


@mcp.tool()
def list_dir(path: str) -> dict:
    """List a directory under /srv/sources-* (names + type: dir/file/link)."""
    return be.list_dir(path)


@mcp.tool()
def resolve_dependency(repo_path: str, name: str, kind: str = "auto",
                       max_results: int = 20) -> dict:
    """Look up a dependency's locked version inside a casket source tree
    (path from resolve_repo/resolve_component). kind: "cargo" (Cargo.lock:
    exact version, source, reverse-dependents), "go" (go.mod: required
    version, indirect flag, replace directives), or "auto" (both). e.g.
    resolve_dependency("/srv/sources-layered-ocp4.20/osc/git/guest-components-...",
    "rustls"). NOTE: Cargo.lock does not record enabled cargo features."""
    return be.resolve_dependency(repo_path, name, kind, max_results)


@mcp.tool()
def permalink(path: str, line: int = 0) -> dict:
    """Build a GitHub permalink for a file in a casket source tree. Resolves
    through INDEX.tsv and SUBMODULES.tsv automatically — the caller never
    needs to grep these files or handle submodule path stripping. Returns
    {url, repo, ref, exact, source, file, mount}. exact=false means the
    submodule ref is a branch-head approximation. url=null when the repo is
    not on GitHub."""
    return be.permalink(path, line)


@mcp.tool()
def coverage_report(version: str) -> dict:
    """Casket coverage summary for an OCP version (minor "4.20" or patch
    "4.20.28"): per-phase component/repo counts, b-operand per-product
    breakdown (cnv/acs/mce/acm/rhoai/odf/quay/osc/trustee), a-rpm SRPM count,
    expected-vs-missing products, and known structural gaps. Check this FIRST
    to avoid searching for sources the casket does not carry (and hand those
    to github-trace instead)."""
    return be.coverage_report(version)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "http":
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = 8765
        # MCP streamable-http enables DNS-rebinding protection by default, which
        # only allows Host: localhost and returns 421 for LAN clients (by IP or
        # hostname). This server is read-only and path-confined, intended for a
        # trusted home LAN, so disable that check to accept LAN Host headers.
        # (Do not expose to untrusted networks — see README "Remote access".)
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
