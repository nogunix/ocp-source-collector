# ocp-source-collector — an offline source corpus for OpenShift releases

[![ci](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml)

ocp-source-collector collects the upstream source for **every component of an
OpenShift release payload** and bundles it into a single archive — an internally
xz-compressed squashfs, `casket-YYYYMMDD-ocp<ver>.sqfs.xz`. Mount it and the
whole release reads as an ordinary directory tree, with no network access.

The problem it solves: when you need to know what a given OpenShift release
actually shipped — the exact commit behind a container image, the source of an
operand three hops down a catalog, the patched source of an RPM on the node OS,
or the version of a Go module vendored into all of it — that information is
scattered across registries, catalogs and package databases that may not be
reachable from where you are, and that change under you when they are.

## What gets collected

There are **two origins × two depths**: A and B each take a source directly
(the release payload, the operator catalog), while A-rpm and B-operand look
one level deeper into the *same* origin.

| Phase | Scope |
|-------|-------|
| **A** | Component git sources, as GitHub archive tarballs |
| **A-rpm** | SRPMs from the rhel-coreos (machine-os) image — one payload image, opened up per RPM |
| **B** | OperatorHub (redhat-operators): file-based catalog, bundle manifests, and github source |
| **B-operand** | Operand sources for layered products (CNV/ACS/MCE/ACM/RHOAI/ODF/Quay) — the same catalog, one hop deeper |

Two cross-cutting layers apply to all of them:

- **Language dependencies** (`deps/`) — Go vendors in-tree most of the time,
  Rust/Node/Python never do. `collect-deps.py` resolves lockfiles and fetches
  Go module zips, crates, npm tarballs and PyPI sdists, so a CVE in `rustls`
  or `lodash` is traceable offline.
- **Submodules** — `git archive` writes a gitlink as an empty directory.
  `collect-submodules.py` recovers the pinned commit from the parent's tree
  object and fills them in.

## Two ways to use it

| Mode | What you need | Where to read |
|------|---------------|---------------|
| **(A) Use a running host's services over the LAN** | A browser or an MCP client | [docs/setup.md](docs/setup.md) §1 |
| **(B) Build the whole thing yourself** | A Linux host **and a valid Red Hat subscription** | [docs/setup.md](docs/setup.md) §2-5 |

Mode (A) has two entry points, both read-only:

- **OpenGrok web UI** at `http://<casket-host>:8080/` — symbol search, cross
  references, source browsing ([opengrok/README.md](opengrok/README.md))
- **casket-mcp** — search and read the sources from Claude or any other MCP
  client ([mcp/README.md](mcp/README.md))

## Repository layout

```
ocp-source-collector/
├── scripts/                 Collection, build and operations scripts (the core)
│   ├── discover.sh / fetch-git.sh / manifest.sh / package.sh   Phase A pipeline
│   ├── phase-a-rpm-*.sh                                        A-rpm pipeline
│   ├── phase-b-*.sh                                            Phase B / B-operand pipeline
│   ├── casket-build.sh / casket-swap.sh / casket-check.sh ...  Release lifecycle drivers
│   ├── collect-deps.py / collect-submodules.py                 Dependency & submodule collection
│   ├── build-source-index.py                                   Source index generation
│   ├── lib.sh / lib-*.sh                                       Shared shell libraries
│   └── registry.py / deplib.py / submodulelib.py               Python libraries
├── config/                  Target version & product definitions
├── docs/                    Design & operations documentation
├── mcp/                     casket-mcp server (search sources via MCP)
├── opengrok/                OpenGrok source browser (Web UI)
├── tests/                   Test suite (runs in CI)
├── systemd/                 Auto-update systemd units
├── containers/              SRPM collection container definitions
├── ansible/                 Host setup playbooks
└── .github/workflows/       CI definitions
```

## Quick start

`$CASKET_WORK` is the environment variable pointing at your checkout of this
repository (default `~/casket-work` in `scripts/lib.sh`, overridable).

```bash
cd "$CASKET_WORK"
V=4.20.22       # any OCP version

./scripts/discover.sh   -v "$V" -a x86_64      # oc adm release info -> images.tsv, commits.tsv
./scripts/fetch-git.sh  -v "$V" --jobs 6       # parallel GitHub archive download
./scripts/manifest.sh   -v "$V"                # MANIFEST.json with sha256/size
./scripts/package.sh    -v "$V" -o /path/to/output   # mksquashfs -> .sqfs.xz
```

Every stage is idempotent and skips work whose output already exists. Use
`--limit N` on `fetch-git.sh` as a smoke test before a full run. Other phases
and their options are in [docs/pipeline.md](docs/pipeline.md).

## Keeping up with new releases

`config/minors.txt` is the single source of truth for which versions are
targeted. Four drivers wrap the per-phase pipeline:

```bash
./scripts/casket-check.sh                 # read-only freshness check, safe to cron
./scripts/casket-build.sh   --apply       # build, register as staged
./scripts/casket-swap.sh    --apply       # point the mounts at the staged artifact (needs sudo)
./scripts/casket-cleanup.sh --apply       # delete retired artifacts past the retention window
```

See [docs/operations.md](docs/operations.md) for the full flow.

## Documentation

| Document | Contents |
|----------|----------|
| [docs/setup.md](docs/setup.md) | **Start here**: use a running host, or build your own |
| [USAGE.md](USAGE.md) | User guide: mount points, layout cheatsheet, common operations |
| [docs/pipeline.md](docs/pipeline.md) | Pipeline reference: scripts, per-phase execution, work directories |
| [docs/operations.md](docs/operations.md) | Operations: check → build → swap → cleanup |
| [docs/artifacts.md](docs/artifacts.md) | What each phase's casket contains, its layout and numbers |
| [docs/design-notes.md](docs/design-notes.md) | Design decisions and their rationale |
| [docs/operational-pitfalls.md](docs/operational-pitfalls.md) | Hard-won operational lessons and their fixes |
| [docs/collection-model.md](docs/collection-model.md) | The formal model behind what "collected" means |
| [mcp/README.md](mcp/README.md) | casket-mcp: MCP server for searching and reading the sources |
| [opengrok/README.md](opengrok/README.md) | OpenGrok source browser (Web UI) |

## Requirements

`oc`, `jq`, `curl`, `awk`, `sha256sum`, `mksquashfs`, `xz`.

**A valid Red Hat subscription is a hard prerequisite** for building: obtaining a
pull secret, pulling operator catalogs from `registry.redhat.io`, registering the
RHEL 9 VM used by A-rpm, and fetching EUS SRPMs all depend on it. Without one,
only Phase A's GitHub fetching works. The pull secret defaults to
`~/.docker/config.json` (override with `AUTHFILE`).

Run `./scripts/casket-doctor.sh --build` to check tools, registry auth and free
space before starting.

## License

MIT — see [LICENSE](LICENSE). The sources a casket *contains* remain under their
own upstream licenses; this repository is the collection machinery, and no
collected artifact is distributed here.
