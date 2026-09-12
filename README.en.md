# ocp-source-collector — an offline source corpus for OpenShift releases

[![ci](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml)

*[日本語版 README](README.md) — the Japanese README is the authoritative and
more detailed document; this page is a complete but shorter overview.*

ocp-source-collector collects the upstream source for **every component of an OpenShift
release payload** and bundles it into a single archive — an internally
xz-compressed squashfs, `casket-YYYYMMDD-ocp<ver>.sqfs.xz`. Mount it and the
whole release reads as an ordinary directory tree, with no network access.

The problem it solves: when you need to know what a given OpenShift release
actually shipped — the exact commit behind a container image, the source of an
operand three hops down a catalog, the patched source of an RPM on the node OS,
or the version of a Go module vendored into all of it — that information is
scattered across registries, catalogs and package databases that may not be
reachable from where you are, and that change under you when they are.

`$CASKET_WORK` below is the environment variable pointing at your checkout of
this repository (default `~/casket-work` in `scripts/lib.sh`, overridable). All
path examples are written in terms of it, so they work for anyone who clones
this repo.

## What gets collected

These are not four peer phases. There are **two origins × two depths**: A and B
each take a source directly (the release payload, the operator catalog), while
A-rpm and B-operand look one level deeper into the *same* origin.

| Phase | Scope | Status |
|-------|-------|--------|
| **A** | Component git sources, as GitHub archive tarballs | Complete |
| **A-rpm** | SRPMs from the rhel-coreos (machine-os) image — one payload image, opened up per RPM | Complete |
| **B** | OperatorHub (redhat-operators): file-based catalog, bundle manifests, and github source | Complete |
| **B-operand** | Operand sources for layered products (CNV/ACS/MCE/ACM/RHOAI/ODF/Quay) — the same catalog, one hop deeper | Complete |

Two cross-cutting layers apply to all of them:

- **Language dependencies** (`deps/`) — a collected archive holds a component's
  own code; whether its dependencies came along was a per-language accident. Go
  vendors in-tree most of the time, Rust/Node/Python never do. `collect-deps.py`
  resolves lockfiles and fetches Go module zips, crates, npm tarballs and PyPI
  sdists, so a CVE in `rustls` or `lodash` is traceable offline.
- **Submodules** — a GitHub `/archive/<sha>.tar.gz` is `git archive` output, and
  `git archive` writes a gitlink as an **empty directory**. Across the fleet,
  1052 trees carried `.gitmodules` with all 2543 submodule directories empty.
  `collect-submodules.py` recovers the pinned commit from the parent's tree
  object and fills them in.

## Two ways to use it

| Mode | What you need | Where to read |
|------|---------------|---------------|
| **(A) Use a running host's services over the LAN** — most people | A browser or an MCP client | [docs/setup.md](docs/setup.md) §1 |
| **(B) Build the whole thing yourself** | A Linux host **and a valid Red Hat subscription** | [docs/setup.md](docs/setup.md) §2-5 |

Mode (A) has two entry points, both read-only, with no copy of the source
landing on your machine:

- **OpenGrok web UI** at `http://<casket-host>:8080/` — symbol search, cross
  references, source browsing ([opengrok/README.md](opengrok/README.md))
- **casket-mcp** — search and read the sources from Claude or any other MCP
  client ([mcp/README.md](mcp/README.md))

## Where the data lives

| Location | Contents | Lifetime |
|----------|----------|----------|
| **`/srv/sources-*`** | **Read the source here.** Read-only mounts of every casket; casket-mcp and OpenGrok both read these | Permanent; the mount set is rebuilt from the two files below |
| `/mnt/hdd/casket-ocp/*.sqfs.xz` | The artifacts themselves, one file per casket | Immutable; old generations are retired in the registry, then deleted by `casket-cleanup.sh` |
| `state/registry.json` + `config/static-mounts.tsv` | **The single source of truth for mounts** (never fstab) | `casket-mounts.sh` reconciles the real mounts against them |
| `$CASKET_WORK/{ocp*,phase-*}/` | Build intermediates | **Disposable** once the casket exists |
| `/srv/opengrok-data/` | OpenGrok index (~150-250G) | Regenerable, but it takes hours — don't delete it |

Disk split on the reference host is **SSD for build work and the OpenGrok index,
HDD for artifacts**. That is a sizing judgement, not a requirement: every
location is configurable (`CASKET_WORK`, `casket-build.sh -o`, and `DATA_VOLUME`
for OpenGrok). Indexing every minor version would cost ~900G, so by default only
the minors in `config/opengrok-minors.txt` are indexed; the rest stay searchable
through casket-mcp's ripgrep backend.

Rough sizes: A ×9 ~6.4G, A-rpm 4.1G, B ×9 ~5.6G, B-operand ×9 ~9.5G,
certified + community ×18 ~19G.

## Quick start

```bash
cd "$CASKET_WORK"
V=4.20.22       # any OCP version

./scripts/discover.sh   -v "$V" -a x86_64      # oc adm release info -> images.tsv, commits.tsv
./scripts/fetch-git.sh  -v "$V" --jobs 6       # parallel GitHub archive download
./scripts/manifest.sh   -v "$V"                # MANIFEST.json with sha256/size
./scripts/package.sh    -v "$V" -o /mnt/hdd/casket-ocp   # mksquashfs -> .sqfs.xz
```

Every stage is idempotent and skips work whose output already exists; to re-run
one, delete its output directory. Use `--limit N` on `fetch-git.sh` as a smoke
test before a full ~164-target run. Other phases, their options and their
runtimes are in [docs/pipeline.md](docs/pipeline.md).

## Keeping up with new releases

`config/minors.txt` and its siblings are the single source of truth for which
versions are targeted, and `state/registry.json` tracks each artifact through
staged → live → retired. Four drivers wrap the per-phase pipeline:

```bash
./scripts/casket-check.sh                 # read-only freshness check, safe to cron
./scripts/casket-build.sh   --apply       # build, register as staged
./scripts/casket-swap.sh    --apply       # point the mounts at the staged artifact (needs sudo)
./scripts/casket-cleanup.sh --apply       # delete retired artifacts past the retention window
```

See [docs/operations.md](docs/operations.md) for the full flow, including why
Phase A's swap is additive while the others replace.

## Documentation

| Document | Contents |
|----------|----------|
| [docs/setup.md](docs/setup.md) | **Start here**: use a running host, or build your own |
| [USAGE.md](USAGE.md) | User guide: mount points, layout cheatsheet, common operations |
| [docs/artifacts.md](docs/artifacts.md) | What each phase's casket contains, its layout and numbers |
| [docs/pipeline.md](docs/pipeline.md) | Pipeline reference: scripts, per-phase execution, work directories |
| [docs/operations.md](docs/operations.md) | Operations: check → build → swap → cleanup |
| [docs/design-notes.md](docs/design-notes.md) | Design decisions and their rationale |
| [docs/operational-pitfalls.md](docs/operational-pitfalls.md) | Hard-won operational lessons and their fixes |
| [docs/collection-model.md](docs/collection-model.md) | The formal model behind what "collected" means |
| [docs/casket-mcp-design.md](docs/casket-mcp-design.md) | casket-mcp design: backend routing, tool surface |
| [CHANGELOG.md](CHANGELOG.md) | Change history |
| [CLAUDE.md](CLAUDE.md) | Developer guide — architecture notes for Claude Code |
| [mcp/README.md](mcp/README.md) | casket-mcp: the MCP server for searching and reading the sources |
| [opengrok/README.md](opengrok/README.md) | The OpenGrok source browser |

Most documents are in Japanese; [CLAUDE.md](CLAUDE.md) is in English and is the
densest description of how the system actually behaves.

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
