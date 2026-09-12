# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ocp-source-collector` collects git sources for every component of an OpenShift release payload and bundles them into a single `casket-YYYYMMDD-ocp<ver>.sqfs.xz` archive that matches the existing RHEL casket format. **All phases are complete and live in production** at `/srv/sources-*` on casket-host.

Phases aren't 4 independent peers: there are 2 origins (release payload, operator
catalog), each with a direct mode and a "go one level deeper" sub-mode. `a-rpm`
and `b-operand` are that deeper mode for `a`/`b` respectively. Both were renamed
on 2026-07-11: first Phase B/D → `a-rpm`/`c-operand` (they're sub-resolutions,
not independent phases), then old Phase C → `b`/`b-operand` (so the two origins
read as sequential A/B instead of A/C) — see README.md's "What gets collected" section for the reasoning.

- **A**: OCP component git tarballs. **9 minors live** (4.14-4.22).
- **A-rpm**: SRPMs from redhat-coreos rpmdb (99.86% coverage).
- **B**: Operator Catalog Bundle (OCB) sources. **9 minors live** (4.14-4.22).
- **B-operand**: layered-product (CNV/ACS/MCE/ACM/RHOAI/ODF/Quay) operand sources, same redhat-operator-index catalog as B, one hop deeper (CSV `relatedImages`).

Three supplementary collection layers run on the STAGE tree (one implementation serves all phases):

- **Dependency sources** (`scripts/collect-deps.py`, `scripts/deplib.py`): Go/Rust/Node/Python dependency archives under `deps/`. See [operational pitfalls: dependency collection](docs/operational-pitfalls.md#dependency-collection) for cross-fs traps and resolver edge cases.
- **Submodule expansion** (`scripts/collect-submodules.py`, `scripts/submodulelib.py`): fills empty submodule dirs left by `git archive`. See [operational pitfalls: submodule collection](docs/operational-pitfalls.md#submodule-collection) for API limits and backfill decisions.
- **Source index** (`scripts/build-source-index.py`): `INDEX.tsv` + `by-component/` + `by-repo/` symlink trees for dedup-name discovery.

Neither `deps/` nor submodule trees are indexed by OpenGrok (decisions 2026-07-29 / 2026-08-19). Search them via ripgrep or casket-mcp.

Rollout to already-built caskets: `scripts/repackage-add-deps.sh` / `repackage-add-submodules.sh` / `repackage-add-index.sh` (overlayfs onto read-only mount + mksquashfs).

## Pipeline (run in order within each phase)

See [docs/pipeline.md](docs/pipeline.md) for full script tables, execution examples, and directory layout.

### Phase A: OCP component sources
```bash
./scripts/discover.sh   -v 4.20.22 [-a x86_64]
./scripts/fetch-git.sh  -v 4.20.22 [--jobs 6] [--limit N]
./scripts/manifest.sh   -v 4.20.22
./scripts/package.sh    -v 4.20.22 [-o /mnt/hdd/casket-ocp]
```

### A-rpm: SRPMS from RHEL coreos
```bash
./scripts/phase-a-rpm-package.sh
```
Requires RHEL 9 VM (`rhel9-srpm` libvirt guest) with `dnf download --source` capability. See [docs/pipeline.md](docs/pipeline.md#a-rpm-pipeline-rhel-coreos-srpm-collection) for the full VM workflow.

### Phase B: Operator sources
```bash
./scripts/phase-b-discover.sh    -v 4.20 [-a x86_64]
./scripts/phase-b-fetch-bundles.sh -v 4.20 [--jobs 6]
./scripts/phase-b-fetch-source.sh  -v 4.20 [--jobs 6]
./scripts/phase-b-resolve-v2.sh    -v 4.20 [--jobs 6]
./scripts/phase-b-package.sh       -v 4.20
```

Stages are idempotent: each skips work whose output already exists. `--limit N` on `fetch-git.sh` is the smoke-test path.

## Release maintenance (check / build / swap / cleanup)

See [docs/operations.md](docs/operations.md) for the full flow, auto-update cadence (3-week cycle), and mount management.

Target versions: `config/minors.txt` (a/b/b-operand), `config/phase-a-rpm-minors.txt`, `config/phase-b-operand-products.tsv`. Lifecycle: `state/registry.json` (managed via `scripts/registry.py`, never hand-edited).

```bash
./scripts/casket-check.sh --phase all          # read-only freshness check
./scripts/casket-build.sh --phase a --unit 4.20 --apply  # build + register staged
sudo ./scripts/casket-swap.sh --phase a --unit 4.20 --apply  # remount + registry live
./scripts/casket-cleanup.sh --apply            # delete retired artifacts
```

See [operational pitfalls: build and storage](docs/operational-pitfalls.md#build-and-storage) for ENOSPC, artifact naming, stage reclaim, thin-casket detection, and freshness-check gotchas.

## Architecture notes worth knowing before editing

- **Source of truth is `oc adm release info --commits`**, not `*-source` container images. Do not reintroduce source-container lookups without re-verifying.
- **Tarballs are deduped by `(repo, commit)`**, not by image name. ~27 of 191 images share repo+commit. Source index (`by-component/`, `by-repo/`) makes the many-to-one mapping discoverable.
- **`.sqfs.xz` is internally-xz-compressed squashfs, NOT an outer xz wrap.** `mksquashfs -comp xz`; do not add an outer `xz` step.
- **`lib.sh`** centralizes arg parsing (`parse_args_version_arch`), logging (`log`/`die`), `require_cmd`, and path resolution (`version_dir`). New scripts should source it.
- **Env vars** (defaults in `lib.sh`): `CASKET_WORK` (work root, default: the checkout itself, derived in `lib.sh`), `AUTHFILE` (registry auth, default `~/.docker/config.json`), `RELEASE_REGISTRY` (default `quay.io/openshift-release-dev/ocp-release`).

## Required tools

`oc`, `jq`, `curl`, `awk`, `sha256sum`, `mksquashfs`, `xz`. `oc` needs a working pull secret at `$AUTHFILE`.

## Storage

Intermediate output goes under `$CASKET_WORK` (root fs). Final `.sqfs.xz` defaults to `/mnt/hdd/casket-ocp/` (HDD). `ocp<VERSION>/` and `phase-a-rpm/srpms/` are disposable once the `.sqfs.xz` exists. See [operational pitfalls](docs/operational-pitfalls.md#build-and-storage) for cross-filesystem traps, disk-space incidents, and stage cleanup procedures.

## Operational pitfalls

Detailed incident reports and hard-won lessons are in [docs/operational-pitfalls.md](docs/operational-pitfalls.md), organized by topic:

- **[Dependency collection](docs/operational-pitfalls.md#dependency-collection)** — cross-fs hardlink trap, manifest matching quirks, malformed lockfile resilience
- **[Submodule collection](docs/operational-pitfalls.md#submodule-collection)** — empty dirs from `git archive`, GitHub API rate limits, index invisibility, backfill decisions
- **[OpenGrok](docs/operational-pitfalls.md#opengrok)** — keep-patches cap, staging tree inode trap, symlink cycle hang, `--disableRepository` persistence
- **[B-operand coverage](docs/operational-pitfalls.md#b-operand-source-coverage)** — empty `git/` causes, resolution strategies, deployment numbers
- **[Build and storage](docs/operational-pitfalls.md#build-and-storage)** — ENOSPC, hardlink farm, thin casket detection, artifact naming, freshness check modes, shell gotchas
