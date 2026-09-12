# Changelog

This project does not use version tags, so entries are grouped by date (newest first).

## 2026-07-12

- **A-rpm fresh complete = all phases now fresh**: Re-extracted rpmdb for 9 patches in
  the rhel9-srpm VM and re-collected SRPMs (EUS/E4S with per-dist-tag `--releasever` +
  enablerepo retry). Made `casket-20260712-ocp-srpms` live (by-ocp now includes 4.21.22
  / 4.22.3 for the first time; bin→src resolution 4983 with 28 unresolved = 99.44%.
  Remaining are grub2/openssh/python3 z-stream rebuilds after EUS EOL).
- `phase-a-rpm-extract-one.sh`: Tolerate deterministic SIGPIPE (rc 141) from rpm2cpio
  (trailing padding write failure after extraction completes was treated as failure by
  pipefail).
- **Switched OpenGrok to minor whitelist mode → moved index back to SSD**: Stopped
  indexing all minors (named volume ~905G) and switched to indexing only those listed
  in `config/opengrok-minors.txt` (default: 4.20/4.21/4.22, 3 minors). Non-indexed
  minors remain searchable via casket-mcp's ripgrep path. Footprint shrank to ~150-250G,
  fitting on SSD, so `DATA_VOLUME` was moved back to `/srv/opengrok-data` (SSD)
  (earlier that day it was temporarily evacuated to `/mnt/hdd/opengrok-data`, but the
  whitelist change made that unnecessary). `run-opengrok.sh` now binds only the
  `/srv/sources-*` mounts actually referenced by the staging tree (stopped binding all
  48 caskets). Removed stale index remnants from 5 retired old-patch projects.
- **Unified OpenGrok startup race / 0-result search bug fix to configuration.xml
  persistence** (root-cause 2026-07-12): When the in-container startup sync lost the
  race against Tomcat's REST deployment, all projects remained `indexed=false`, causing
  xref to work normally but full-text/symbol search to **silently return 0 results for
  any query** (log: `IndexNotFoundException: no segments*`). `run-opengrok.sh` now
  persists `/opengrok/etc` (configuration.xml) to `ETC_VOLUME` (default
  `/srv/opengrok-etc`), so after the first sync completes, the indexed config survives
  container recreation and restarts, preventing the race from resurfacing. If the
  0-result bug occurs, the correct recovery is to re-run `run-opengrok.sh` (recreate +
  re-run sync). Manual recovery via `PUT .../indexed` is prohibited as it triggers full
  suggester rebuild causing OOM (documented in README).
  Note: A `wait-for-ready.sh` (+ `opengrok.service` `ExecStartPost`) that re-triggered
  sync on every startup was added temporarily but removed once config persistence made
  it unnecessary. Nightly suggester rebuild disabling was moved back into
  `run-opengrok.sh`.
- **ansible**: Changed `casket-mcp` / `opengrok` to `restarted` (was `started` only)
  when unit files change, so updated `ExecStart` takes effect within the same playbook
  run.
- **Documentation premise clarification**: Caskets are not distributed as files (usage
  is either via services on the running host or self-hosted build; docs/setup.md fully
  rewritten). Red Hat subscription requirement explicitly stated in README / setup.md.
- CHANGELOG additions, scratch/ throwaway script cleanup.

## 2026-07-11 (continued)

- **Completed full fresh rebuild plan for all phases** (approach: replace with new
  builds rather than backfilling existing caskets into registry):
  - Phase A ×9 (4.14.58–4.22.3, **4.22 newly tracked**) → live
  - B ×9 / B-operand ×9 (resolution overhaul + same-day resolver improvements) → live
  - Deployed certified / community catalogs ×18 (reusing phase-b's 5 scripts via
    `CATALOG` env var; manual rebuild workflow, not included in auto-update)
  - Deleted old-generation caskets (20260523–0608), freed ~57G+
- **Eliminated fstab**: Removed 50+ casket fstab entries, migrated to
  `casket-mounts.sh` reconciler + boot-time `casket-mounts.service` using
  `state/registry.json` + `config/static-mounts.tsv` as single source of truth.
  `casket-swap.sh` simplified to "in-place remount + registry transition".
- **Auto-update mechanism**: systemd user timer (daily 06:00) runs check→build(staged)
  unattended. Swap remains manually gated. b/b-operand enabled after registry setup.
- **CNV downstream gap investigation** (JANUS case): Rescued 4 of 5 unresolved
  components via public paths (ipam-extensions commit match, virt-artifacts-server
  kubevirt monorepo determination, hostpath sibling-version fixup). virt-core's
  downstream diff has ftp.redhat.com kubevirt SRPM as the only public path, but it
  only tracks GA releases, so incorporation was deferred (recorded in design-notes).
- **Generalization**: `CASKET_OUT` env var, mount service installer, new-host
  diagnostic `casket-doctor.sh`, docs/setup.md created.
- Updated casket-mcp phase identifiers to new scheme (a/a-rpm/b/b-operand/b-certified/
  b-community), fixed catalog mount classification gap.
- Bug fixes: `lib-fingerprint.sh` arch filter (`linux/x86_64`→`linux/amd64`, a latent
  bug that would break all b/b-operand builds), `CASKET_WORK` resolution under sudo
  ($HOME-derived→checkout-derived + export), preventing root ownership of registry.json,
  community catalog `oc image info` hang (timeout 60s), missing execute bit on
  `casket-*.sh`.
- OpenGrok: staging certified/community under dedicated project names, README updated
  for ~54 project configuration, documented rootless requirement (sudo startup hits
  Docker Hub rate limits).

## 2026-07-11

- **Branch integration**: Merged `casket-source-index` into `main` (PR #5, #6, #7).
- **Phase renaming** (2 stages): Old Phase B/D → `a-rpm`/`c-operand` → final
  `b`/`b-operand`. Organized the 2 origins (release payload / operator catalog) × 2
  depth levels so that `A`/`A-rpm` and `B`/`B-operand` read as sequential numbering.
  `phase-b/` work directory moved to `phase-a-rpm/`.
- README / CLAUDE.md / USAGE.md fully revised for renaming, host-specific paths
  generalized.
- **Public release preparation**: Added MIT LICENSE, generalized casket-host IP/hostname,
  removed customer case identifiers, excluded `decks/` (internal template artifacts with
  license restrictions) from tracking, cleared machine-specific server config from
  `.mcp.json`.
- **Release operations automation**: Added `casket-check/build/swap/cleanup.sh`,
  consolidated `config/minors.txt` / `config/phase-a-rpm-minors.txt` /
  `config/phase-b-operand-products.tsv` as single source of truth for target
  minors/products. `scripts/registry.py` for casket artifact lifecycle tracking.
- `lib-fingerprint.sh`: Shared `mount_path_for` and freshness fingerprint retrieval.
- opengrok: `WORKERS` to cap parallel JVM count during reindex.
- Fixed `swap-source-index.sh` fstab rewrite no-op bug (`#` escaping issue).

## 2026-06-10

- opengrok: Made webapp heap (`CATALINA_OPTS`) configurable. Documented heap settings
  and reindex hang troubleshooting.

## 2026-06-09

- **Phase C (now B) resolution overhaul**: Fixed 3 resolution bugs suppressing
  uncollected operators, hardened bundle fetching, extracted resolution logic to
  `lib-resolve.sh` for use by `resolve-v2.sh`.
- **Phase D (now B-operand)**: Added operand source fetching scripts for layered
  products (CNV/ACS/MCE/ACM/RHOAI/ODF/Quay).
- opengrok airgap: Compressed index with zstd, eliminating bloat from sparse zeros.
- CI: Added lint and network-free regression tests.
- Repository cleanup: Separated `decks/` and `analysis/` from root, gitignored work
  directories.
- `swap-operators-remount.sh`: 4.21 support, busy-loop fallback added.
- README/CLAUDE.md: Recorded Phase C resolution overhaul, added Phase D documentation,
  documented source-index (`INDEX.tsv` + `by-component`/`by-repo`).

## 2026-06-08

- **casket-mcp** implementation: stage 1 (FS navigation + ripgrep MCP server),
  stage 2 (OpenGrok REST backend: symbol/xref/full-text search),
  stage 3 (`diff_file` + Phase D OpenGrok indexing).
  Documented HTTP access for LAN/remote.
- casket: Added source-index generation (`by-component`/`by-repo`/`INDEX.tsv`) at
  package time, overlay backfill + swap tools, applied to all 24 caskets in production.
- phase-b (now B): Added applied-tree mode (`PHASE_B_APPLY=1`, `rpmbuild -bp`),
  `.git` removal, shipped caskets with `KEEP_STAGE` reuse support.
- opengrok: Skip unnecessary `chown -R` on 215G data volume when already owned by
  appuser.

## 2026-06-02

- opengrok: Finalized airgap deploy scripts and documentation.

## 2026-06-01

- Initial commit.
