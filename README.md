# ocp-source-collector — an offline source corpus for OpenShift releases

[![ci](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/github/license/nogunix/ocp-source-collector)](LICENSE)
[![codecov](https://codecov.io/gh/nogunix/ocp-source-collector/branch/main/graph/badge.svg)](https://codecov.io/gh/nogunix/ocp-source-collector)
[![ShellCheck](https://img.shields.io/badge/lint-ShellCheck-brightgreen)](https://www.shellcheck.net/)
[![Ruff](https://img.shields.io/badge/lint-Ruff-purple)](https://docs.astral.sh/ruff/)

**Turn an entire OpenShift release into source code you can read offline.**

ocp-source-collector collects the source code for every component of an
OpenShift release payload and packages it into a mountable squashfs archive
(`casket-YYYYMMDD-ocp<ver>.sqfs.xz`). No network required — one `mount` and
the whole release is an ordinary directory tree.

### What's inside

| What | Scale |
|------|-------|
| OCP component sources | Git source tree at the exact build commit for every payload image (~190 images/version) |
| Node OS SRPMs | Source RPMs for every RPM on rhel-coreos — kernel, cri-o, systemd, … (843 SRPMs, 99.86% coverage) |
| Operator sources | FBC catalog + bundle manifests + git sources from redhat-operator-index (~120 operators/version) |
| Layered-product operand sources | Operand image sources for **191 products** including CNV, ACS, MCE, ACM, RHOAI, ODF, and Quay |
| Language dependencies | Go modules, Rust crates, npm packages, and PyPI sdists resolved from lockfiles |
| Submodule trees | Git submodules recovered from empty directories left by `git archive` |

**9 minor versions** (4.14–4.22) tracked continuously, auto-updated on a 3-week cycle.

### Use cases

- **Crash investigation** — look up the exact source for a stack trace without chasing registries
- **CVE impact analysis** — find which versions and components contain vulnerable code
- **Cross-version diffing** — compare component source changes across OpenShift releases
- **Supply-chain visibility** — trace vendored Go/Rust/Node/Python dependencies end to end

### How to search

| Method | Description |
|--------|-------------|
| [OpenGrok](opengrok/README.md) | Web UI for full-text source search and browsing |
| [casket-mcp](mcp/README.md) | MCP server for Claude Code / AI agents |
| `ripgrep` | grep the mounted directory tree directly |

### The problem it solves

When you need to know what a given OpenShift release actually shipped — the
exact commit behind a container image, the source of an operand three hops
down a catalog, the patched source of an RPM on the node OS, or the version
of a Go module vendored into all of it — that information is scattered across
registries, catalogs and package databases that may not be reachable from
where you are, and that change under you when they are.

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
repository (`scripts/lib.sh` derives it from the checkout location, so it
follows the directory wherever it is; override it to point elsewhere).

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

## Prerequisites

### System

| Requirement | Purpose | Required? |
|-------------|---------|-----------|
| Bash 4+ | All pipeline scripts | **Yes** |
| Python 3.9+ | `collect-deps.py`, `collect-submodules.py`, `build-source-index.py`, `registry.py` | **Yes** |
| Red Hat subscription | Pull secret, operator catalogs, A-rpm SRPMs (container registers via activation key), EUS SRPMs | **Yes** (Phase A GitHub-only without it) |

### CLI tools

| Tool | Purpose | Required? |
|------|---------|-----------|
| `oc` | `oc adm release info --commits` — the source of truth for payload components | **Yes** |
| `jq` | JSON processing throughout the pipeline | **Yes** |
| `curl` | GitHub archive downloads, API calls | **Yes** |
| `mksquashfs` | Final `.sqfs.xz` archive creation (`-comp xz`) | **Yes** |
| `xz` | Compression (used by mksquashfs internally) | **Yes** |
| `sha256sum` | Manifest checksums | **Yes** |
| `awk` | Text processing in shell scripts | **Yes** |
| [ShellCheck](https://www.shellcheck.net/) | Shell linting (CI) | For development |
| [Ruff](https://docs.astral.sh/ruff/) | Python linting (CI) | For development |
| `pytest` + `pyyaml` | Python test suite | For development |

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CASKET_WORK` | This checkout (derived in `lib.sh`) | Work root for all phases |
| `AUTHFILE` | `~/.docker/config.json` | Registry pull secret |
| `RELEASE_REGISTRY` | `quay.io/openshift-release-dev/ocp-release` | OCP release image registry |
| `CASKET_OUT` | `/mnt/hdd/casket-ocp` | Final `.sqfs.xz` output directory |
| `CASKET_DEP_STORE` | `$CASKET_WORK/dep-store` | Language dependency archive store |
| `CASKET_SUBMODULE_STORE` | `$CASKET_WORK/submodule-store` | Submodule archive store |

Run `./scripts/casket-doctor.sh --build` to check tools, registry auth and free
space before starting.

## License

MIT — see [LICENSE](LICENSE). The sources a casket *contains* remain under their
own upstream licenses; this repository is the collection machinery, and no
collected artifact is distributed here.
