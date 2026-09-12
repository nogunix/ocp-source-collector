# casket-mcp

MCP server exposing the OpenShift / CNV / RHEL source caskets mounted at
`/srv/sources-*` for search and reading. Design: `../docs/casket-mcp-design.md`.

`$CASKET_WORK` below is this repo's checkout root (default `~/casket-work`,
see `../scripts/lib.sh`).

**Stage 1 + 2 (current).** Stage 1 = filesystem navigation + ripgrep. Stage 2 =
OpenGrok REST for symbol-definition / cross-reference / fast broad full-text
(all phases: a / a-rpm / b / b-certified / b-community / b-operand), with graceful degradation when the OpenGrok container is down.
Navigation uses the `by-repo`/`by-component`/`INDEX.tsv` index layer (live since
2026-06-08).

**Key performance fact:** the casket mounts are xz-compressed squashfs — a
ripgrep scan of a whole mount takes ~2 min (decompression). So broad search goes
through OpenGrok's prebuilt index; ripgrep is used only **scoped to a `path`**
(resolve first, then grep — ~seconds). Whole-mount ripgrep is deliberately
refused with guidance. OpenGrok indexes **all phases incl. b-operand** (projects
`ocp-<patch>` / `operators-<minor>` / `srpms` / `layered-<minor>`, the last added
2026-06-08); the scoped-ripgrep path remains for any tree or when OpenGrok is down.

## Tools

| Tool | What it does |
|---|---|
| `list_versions()` | Mounted caskets with phase (A/B/C/D) + version |
| `list_components(version, phase?)` | Component trees from each mount's `git/INDEX.tsv` |
| `resolve_repo(repo, version)` | Upstream repo → on-disk path (via `by-repo/`; defeats dedup naming, e.g. kubevirt core hidden under `libguestfs-tools-*`) |
| `resolve_component(name, version)` | Component/operand → on-disk path (via `by-component/`) |
| `search_symbol(name, project?, path?)` | Symbol **definitions** (OpenGrok `def`). Scoped-ripgrep fallback when OpenGrok down (needs `path`) |
| `search_refs(name, project?)` | Symbol **references / xref** (OpenGrok). Requires OpenGrok (ripgrep can't distinguish refs) |
| `search_text(query, version?\|path?, engine?, project?, max?)` | Full-text. `engine` auto/opengrok/ripgrep. `path`→scoped ripgrep (all phases); else OpenGrok |
| `grep(pattern, path, glob?, max?)` | Regex within a path; `glob` filters files |
| `read_file(path, start?, end?)` | Read a file / line range (read-only, capped) |
| `list_dir(path)` | Directory listing |

All read-only; paths are confined to `/srv/sources-*` (`backends.safe_path`).
OpenGrok endpoint via `OPENGROK_URL` (default `http://localhost:8080`); start it
with `../opengrok/scripts/run-opengrok.sh`. Project names: `ocp-<patch>` (A),
`operators-<minor>` (C), `srpms` (B).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # mcp[cli]
# host needs ripgrep:  sudo dnf install ripgrep
```

## Run

```bash
.venv/bin/python casket_mcp.py        # stdio (default; for Claude Code)
.venv/bin/python casket_mcp.py http   # streamable-http on 0.0.0.0:8765
```

## Register with Claude Code

A project `.mcp.json` is checked in at the repo root (stdio). Claude Code in this
directory will offer to enable the `casket` server. Or add it explicitly:

```bash
claude mcp add casket -- "$CASKET_WORK"/mcp/.venv/bin/python \
  "$CASKET_WORK"/mcp/casket_mcp.py
```

## Remote access from other PCs on the home LAN (HTTP)

The server speaks MCP **streamable-http** so other machines (other Claude Code /
Claude Desktop installs) can use it over the LAN. The host here is **casket-host =
`<CASKET_HOST_IP>`** (br0); port **8765**.

### 1. Run the server in HTTP mode on the host (persistent)

Use the provided systemd **user** service (survives logout, auto-restart):

```bash
cp "$CASKET_WORK"/mcp/casket-mcp.service ~/.config/systemd/user/
loginctl enable-linger "$USER"            # let it run without an active login
systemctl --user daemon-reload
systemctl --user enable --now casket-mcp
systemctl --user status casket-mcp        # journalctl --user -u casket-mcp -f
```

(Quick test instead of a service: `mcp/.venv/bin/python mcp/casket_mcp.py http`.)

`.venv` is gitignored, so if it's ever wiped (stray `rm`, disk cleanup) the
service's `ExecStart` python no longer exists — systemd reports
`status=203/EXEC` and `Restart=on-failure` retries every few seconds forever,
with no other symptom. `ensure-venv.sh` runs as `ExecStartPre` and rebuilds
`.venv` from `requirements.txt` when missing, so this self-heals; if you're
running the server ad hoc (not via systemd) run `./ensure-venv.sh` first or
just redo the Setup step above.

### 2. Open the firewall on the host (once)

```bash
sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload
# verify:  sudo firewall-cmd --query-port=8765/tcp   ->  yes
```

### 3. Point a client PC at it

Either per-project `.mcp.json` (or `~/.claude.json` for global) on the **other** PC:

```json
{ "mcpServers": { "casket": { "type": "http", "url": "http://<CASKET_HOST_IP>:8765/mcp" } } }
```

or via CLI on the other PC:

```bash
claude mcp add --transport http casket http://<CASKET_HOST_IP>:8765/mcp
claude mcp list      # casket ... ✓ Connected
```

(Use a hostname instead of the IP if your LAN has DNS/mDNS, e.g.
`http://<casket-host>:8765/mcp`.)

### Notes / caveats

- **Read-only & path-confined.** All tools only read under `/srv/sources-*`
  (`backends.safe_path`), so exposure is low-risk — but **streamable-http has no
  auth**: anyone who can reach `<CASKET_HOST_IP>:8765` can query it. Keep it on the
  trusted home LAN only (don't port-forward to the internet). For auth/TLS, front
  it with a reverse proxy (nginx/caddy) — not included here. (HTTP mode disables
  MCP's DNS-rebinding protection so LAN clients aren't rejected with HTTP 421 —
  another reason to keep it LAN-only.)
- **OpenGrok dependency is host-side.** `search_symbol`/`search_refs` and broad
  `search_text` need the OpenGrok container running **on casket-host**
  (`../opengrok/scripts/run-opengrok.sh`); the client PC needs nothing extra.
  `resolve_*`/`grep`/`read_file`/`diff_file` work regardless.
- **The client does NOT need the repo, venv, ripgrep, or the casket mounts** —
  all execution happens on casket-host. Only the URL is required.
- One shared server handles many clients concurrently.

## Typical flow

```
resolve_repo("kubevirt/kubevirt", "4.20")   -> path to the real kubevirt tree
grep("DefaultAMD64MachineType", <that path>, glob="*.go")
read_file(<hit path>, <line>, <line>)
```

## Roadmap

- **Stage 2**: OpenGrok REST backend for `search_symbol` / `search_refs` /
  `search_text` (definitions + xref), with ripgrep fallback when OpenGrok is
  down. Requires the OpenGrok
  container running (`../opengrok/scripts/run-opengrok.sh`).
- **Stage 3**: `diff_file` (cross-version API diffs). b-operand is in the
  OpenGrok index now (`layered-<minor>` projects), so the ripgrep fallback is
  no longer carrying it.
