# casket-mcp design notes (ocp-source-collector MCP server)

Exposes casket mounts (`/srv/sources-*`) as MCP tools, enabling Claude and other clients to search and read OpenShift/CNV/RHEL sources. Started: 2026-06-08.

## Design decisions

- **Backend = hybrid**: Search for symbol definitions/references/full text uses OpenGrok REST; navigation, repo resolution, file reads, and uncovered phases use FS + ripgrep + index layer.
- **Transport = dual-mode**: Same implementation switches between stdio (local) and HTTP (LAN sharing).
- **Read-only**: All tools are read-only. Paths restricted to `/srv/sources-*` (traversal prevention).
- Implementation: Python + official `mcp` SDK's **FastMCP** (decorator-based tool definitions).

## Architecture

```
Claude Code / Desktop ──(MCP: stdio or HTTP)──▶ casket-mcp (FastMCP)
                                                  ├─ OpenGrok REST  http://localhost:8080/api/v1
                                                  │     def / symbol / full / path  (indexed phases)
                                                  └─ FS + ripgrep   /srv/sources-*
                                                        by-repo / by-component / INDEX.tsv (index layer)
                                                        read_file / grep (all mounts)
```

### Backend routing

| Operation | Primary | Fallback / supplement |
|---|---|---|
| Symbol definition (`def`) | OpenGrok REST | If unavailable, ripgrep approximation with `type X struct`/`func X` |
| Reference search (xref/symbol) | OpenGrok REST | (rg cannot approximate → returns "OpenGrok is down") |
| Full text search | OpenGrok REST (indexed minors) | Non-indexed minors use ripgrep |
| Repo/component resolution | FS: `by-repo/`, `by-component/`, `INDEX.tsv` | — |
| File read / dir listing | FS direct | — |
| Pattern grep (scoped) | ripgrep | — |

> **Key assumption**: The indexing boundary is **per minor, not per phase**. b-operand is also indexed as `layered-<minor>`, so the earlier "Phase D uses ripgrep" note is resolved. Instead, `config/opengrok-minors.txt` limits which minors are indexed (full indexing would cost ~900G), and `keep-patches: N` limits Phase A / a-rpm patches to the newest N. **Non-indexed minors and patches use the ripgrep path** — this is a permanent design decision based on capacity constraints. The OpenGrok container is assumed to be always running (search degrades when it's down).

## Tool surface

```
# Navigation / resolution (FS, always available)
list_versions()                         -> available OCP minor/patch and phase list (from mount names)
list_components(version, phase?)        -> component list from INDEX.tsv/images.tsv
resolve_repo(repo, version)             -> real path via by-repo/ (resolves dedup naming)
resolve_component(name, version)        -> real path via by-component/

# Search (hybrid)
search_symbol(name, project?)           -> definition locations [{path,line,snippet}] (OpenGrok def)
search_refs(name, project?)             -> references (OpenGrok symbol/xref)
search_text(query, project?|path?, max) -> full text (OpenGrok preferred / rg for uncovered phases)
grep(pattern, path, glob?, max)         -> ripgrep (scoped, all phases)

# Read (FS)
read_file(path, start?, end?)           -> file/range read
list_dir(path)                          -> directory listing

# Useful for investigations (optional, phase 2)
diff_file(path_a, path_b)               -> unified diff between two versions (API diffing etc.)
```

Return values are structured JSON optimized for LLM consumption (includes `path:line` for Claude Code click-through).

## Transport switching

FastMCP's `mcp.run(transport=...)` provides dual-mode from the same codebase:

```python
# end of casket_mcp.py
import sys
mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
if mode == "http":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8765)
else:
    mcp.run(transport="stdio")
```

### Registering with Claude Code

```bash
# Local (stdio). $CASKET_WORK is this repo's checkout (default ~/ocp-source-collector)
claude mcp add casket -- python "$CASKET_WORK"/mcp/casket_mcp.py

# Or via .mcp.json (checked into repo for team sharing, use absolute paths)
{ "mcpServers": {
    "casket": { "command": "python",
                "args": ["/path/to/ocp-source-collector/mcp/casket_mcp.py"] } } }

# LAN sharing (HTTP). Server: python casket_mcp.py http
{ "mcpServers": {
    "casket": { "type": "http", "url": "http://casket-host:8765/mcp" } } }
```

For HTTP mode, open the firewall as with OpenGrok (`firewall-cmd --add-port=8765/tcp`).

## OpenGrok REST reference (v1)

- `GET /api/v1/projects` → project name list (`ocp-4.18` etc.)
- `GET /api/v1/search?def=<sym>&projects=<p>&maxresults=N` → definitions
- Same with `?symbol=` for references / `?full=` for full text / `?path=` for paths
- Response: `{ "resultCount":N, "results": { "<file>": [ {"lineNumber","line","tag"} ] } }`
  (field names may vary by version — verify against the running instance)

## Implementation layout

```
mcp/
├── casket_mcp.py        FastMCP core (tool definitions + routing)
├── backends.py          opengrok_search() / rg_search() / fs_resolve()
├── requirements.txt     mcp[cli]  (ripgrep/curl are OS-level)
└── README.md            Registration instructions, operations (OpenGrok dependency, firewall)
```

Phases:
1. **Phase 1 (FS+rg core)**: list/resolve/read/grep/search_text(rg) — works without OpenGrok for all phases.
2. **Phase 2 (OpenGrok integration)**: search_symbol/refs/text prefer REST. Down-detection degrades to rg.
3. **Phase 3 (optional)**: diff_file, additional OpenGrok indexing to reduce fallback scope.

## Design notes

- Path validation: received `path` is `realpath`'d and must be under `/srv/sources-*` (prevents external reads).
- Result size control: `maxresults` / `read_file` range requirement prevents context overflow.
- by-repo/by-component assumes the **2026-06-08 index layer** — absent on un-rebuilt caskets, but all 24 are now updated.
- When OpenGrok is not running, search responses don't silently degrade — they include `"backend":"ripgrep(opengrok down)"` explicitly.
