# Pipeline reference

Script structure and execution steps for each phase. For artifact contents see [artifacts.md](artifacts.md), for release maintenance operations see [operations.md](operations.md).

## Script structure

`$CASKET_WORK/scripts/`

| File | Role |
|------|------|
| `lib.sh`                  | Common helpers (argument parsing, logger, path resolution) |
| `discover.sh`             | Phase A: Generate images.tsv / commits.tsv from `oc adm release info` |
| `fetch-git.sh`            | Phase A: Parallel download of tarballs from the GitHub archive API, deduped by (repo, commit) |
| `manifest.sh`             | Phase A: Generate MANIFEST.json (with SHA256, sizes) |
| `package.sh`              | Phase A: Output `.sqfs.xz` via mksquashfs |
| `phase-a-rpm-package.sh`  | A-rpm: Consolidate `phase-a-rpm/srpms/` + `phase-a-rpm/rpmdb/` into a single `.sqfs.xz` |
| `phase-b-discover.sh`     | Phase B: Extract FBC `/configs` from redhat-operator-index + generate operators.tsv/bundles.tsv |
| `phase-b-fetch-bundles.sh`| Phase B: Pull each operator's default-channel head bundle, extract manifests/metadata, generate containers.tsv (retains rows even without `containerImage` annotation — prerequisite for csv_repo rescue) |
| `phase-b-fetch-source.sh` | Phase B: Read `vcs-ref` + `io.openshift.build.source-location` from each containerImage, fetch github tarball (v1) |
| `phase-b-resolve-v2.sh`   | Phase B v2: Rescue v1 misses using CSV `annotations.repository` / `spec.links[]` + per-repo ref strategies / tag/branch fallback, appending to `20-git/` |
| `lib-resolve.sh`          | Phase B: Pure functions for source resolution (`normalize_github` / `candidate_source_urls`). Side-effect-free, sourceable, regression-tested in `tests/` |
| `phase-b-package.sh`      | Phase B: Package catalog + bundles + git + meta into a single `.sqfs.xz` (meta/MANIFEST/INDEX prefer `git-v2.tsv`) |
| `phase-b-operand-discover.sh`     | B-operand: Pull layered product head bundles, enumerate CSV `spec.relatedImages` into `images.tsv` |
| `phase-b-operand-resolve-labels.py`| B-operand: Resolve operand images to upstream github source (`upstream-vcs-url`/`component-map`/`oci-source`/`source-location`/`url`) |
| `phase-b-operand-fetch-source.sh` | B-operand: Fetch operand github archives, deduped by (repo, ref\|tag) |
| `phase-b-operand-cargo-vendor.sh` | B-operand: Resolve `Cargo.lock` for Rust products (crates.io `.crate` + github archive for git deps) into `30-vendor/<comp>/<crate>-<ver>/`. Targets listed in `config/cargo-vendor.txt` (currently trustee-operator only). Downloads cached in `$CASKET_WORK/cargo-cache/`, shared across minors |
| `phase-b-operand-package.sh`      | B-operand: Stage and package a single product's operand source into `.sqfs.xz` (`--stage-only` for integration). Includes `30-vendor/` as `vendor/` if present |
| `phase-b-operand-combine.sh`      | B-operand: Consolidate 7 product stages for one minor via `cp -al`, then mksquashfs into an integrated casket (one subdir per product) |
| `build-source-index.py`   | All phases: Generate `git/INDEX.tsv` + `by-component/` + `by-repo/` reverse-lookup index from staged `meta/MANIFEST.json` (auto-run by package scripts after staging). Addresses the problem of deduped directory names hiding the actual repos |
| `swap-operators-v2.sh`             | (ops) Rewrite fstab + restart .mount for new operator caskets (sudo, one-time) |
| `swap-operators-v2-remount-only.sh`| (ops) Remount only, when fstab is already updated |
| `fix-perms-rebuild.sh`             | (ops) Fix permissions on old operator caskets (chmod a+rX then repack) |
| `repackage-add-index.sh`          | (ops) Retrofit index layer onto existing caskets: overlay `INDEX.tsv`/`by-component/`/`by-repo/` onto read-only mount via overlayfs, then re-mksquashfs (handles both single A/B and per-product layered B-operand layouts) |
| `swap-source-index.sh`            | (ops) Swap fstab to indexed caskets + restart mounts (dry-run by default, `--apply` to execute). All 24 caskets deployed as `casket-20260608-*` on 2026-06-08 |

A-rpm auxiliary files under `$CASKET_WORK/phase-a-rpm/vm/`:

| File | Role |
|------|------|
| `bootstrap.sh`                    | Host side (Fedora): Start RHEL 9 cloud VM (cloud-init seed + virt-install) |
| `user-data` / `meta-data`         | cloud-init seed (create cloud-user, inject SSH key) |
| `scripts/01-extract-rpmdb.sh`     | In VM: `oc adm release info --rpmdb --rpmdb-image=rhel-coreos` for 7 versions |
| `scripts/02-fetch-srpms.sh`       | In VM: Union all versions → batch `dnf download --source` |
| `scripts/machineos-pullspecs.tsv` | 7 OCP versions → rhel-coreos image pullspec map (reference) |

### Pipeline (reproducible for any OCP version)

```bash
cd "$CASKET_WORK"
V=4.20.22       # any version

./scripts/discover.sh   -v "$V" -a x86_64
./scripts/fetch-git.sh  -v "$V" --jobs 6
./scripts/manifest.sh   -v "$V"
./scripts/package.sh    -v "$V" -o /mnt/hdd/casket-ocp
```

Options:
- `discover.sh -a {x86_64|aarch64|ppc64le|s390x|multi}` — architecture selection
- `fetch-git.sh --limit N` — fetch only N items for smoke testing
- `fetch-git.sh --jobs N` — parallelism (default 6)
- `package.sh -o <dir>` — output directory (default `/mnt/hdd/casket-ocp`)

### Batch multiple versions

Example: process the latest patch version from each stable-N channel sequentially:

```bash
cd "$CASKET_WORK"

# Get the latest patch version for each stable-N
for v in 4.20 4.19 4.18 4.17 4.16 4.15 4.14; do
  curl -sSL "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable-${v}/release.txt" \
    | awk '/^ *Version:/ {print $2; exit}'
done

# Run the pipeline sequentially for the versions obtained above
for V in 4.19.31 4.18.41 4.17.53 4.16.55 4.15.59 4.14.58; do
  ./scripts/discover.sh   -v "$V" -a x86_64 \
  && ./scripts/fetch-git.sh  -v "$V" --jobs 6 \
  && ./scripts/manifest.sh   -v "$V" \
  && ./scripts/package.sh    -v "$V" -o /mnt/hdd/casket-ocp \
  || { echo "FAILED $V"; break; }
done
```

Measured: ~12 minutes for 7 versions (~2 min per version, as of 2026-05-27). The extracted layout significantly reduced size compared to the tarball-bundled version at ~18.7 GB.

### A-rpm pipeline (rhel-coreos SRPM collection)

A sub-phase that drills into the `rhel-coreos` image from the same release payload as Phase A, at the RPM level. Runs `dnf download --source` on a RHEL 9 machine with a Red Hat subscription (this project uses the libvirt VM `rhel9-srpm`). Only the bootstrap is executed from the Fedora host.

```bash
# 1. Start the RHEL 9 VM (Fedora host side, requires sudo)
cd "$CASKET_WORK"/phase-a-rpm/vm
./bootstrap.sh                           # → ssh cloud-user@<IP>

# 2. Transfer required files to the VM (from Fedora host)
VMIP=192.168.122.xxx                     # shown in bootstrap.sh's final output
scp ~/.docker/config.json cloud-user@$VMIP:~/pull-secret.json
scp -r scripts cloud-user@$VMIP:~/

# 3. In the VM: register subscription → extract → fetch (as cloud-user)
ssh cloud-user@$VMIP
sudo rhc connect --activation-key <KEY> --organization <ORG>   # or subscription-manager register
cd ~/scripts
./01-extract-rpmdb.sh                    # rpmdb for 7 versions (~3 min, including quay pulls)
./02-fetch-srpms.sh                      # batch SRPM download (~15-30 min)
exit

# 4. Collect results to Fedora host + package
rsync -a cloud-user@$VMIP:~/scripts/srpms/    $CASKET_WORK/phase-a-rpm/srpms/
rsync -a cloud-user@$VMIP:~/scripts/rpmdb-tsv/ $CASKET_WORK/phase-a-rpm/rpmdb/
rsync -a cloud-user@$VMIP:~/scripts/{wishlist.txt,missing*.txt,fetch*.log} \
                                              $CASKET_WORK/phase-a-rpm/logs/
./scripts/phase-a-rpm-package.sh -o /mnt/hdd/casket-ocp
```

Measured (7 versions, initial run):
- VM bootstrap: 5 min (qcow2 clone + cloud-init)
- 01-extract-rpmdb.sh: ~3 min (oc pulls rhel-coreos + rpmdb extraction)
- 02-fetch-srpms.sh: ~15 min (including initial dnf cache) — 70-80% hit rate
- EUS / E4S / fast-datapath retry: ~5 min (mostly resolved with `--releasever=9.X`)
- rsync + package: ~3 min

### Phase B pipeline (redhat-operators collection)

```bash
cd "$CASKET_WORK"
for V in 4.20 4.19 4.18 4.17 4.16 4.15 4.14; do
  ./scripts/phase-b-discover.sh      -v "$V" \
  && ./scripts/phase-b-fetch-bundles.sh -v "$V" --jobs 6 \
  && ./scripts/phase-b-fetch-source.sh  -v "$V" --jobs 6 \
  && ./scripts/phase-b-resolve-v2.sh    -v "$V" --jobs 8 \
  && ./scripts/phase-b-package.sh       -v "$V" -o /mnt/hdd/casket-ocp \
  || { echo "FAILED $V"; break; }
done
```

Phase B operates per OCP **minor** (unlike Phase A's per-patch granularity; the catalog tag is `v<minor>`). Measured: ~90 minutes for 7 versions (v1 portion; 5-20 min per version, label lookup and FBC extraction are the bottleneck). `phase-b-resolve-v2.sh` takes 20-30 seconds per version, `phase-b-package.sh` takes 30 seconds to 1 minute per version.

#### certified / community catalogs (added 2026-07-11)

The `CATALOG` environment variable switches the same 5 scripts to `certified-operator-index` / `community-operator-index` (default `redhat`):

```bash
CATALOG=certified ./scripts/phase-b-discover.sh -v 4.20   # pass CATALOG to subsequent scripts as well
```

- Work dirs are `phase-b-certified/<minor>/` / `phase-b-community/<minor>/`, artifacts are `casket-<date>-ocp<minor>-{certified,community}-operators.sqfs.xz` (redhat remains unlabeled as before).
- Measured (4.20): certified 185 operators → **800 MB** / community 300 operators (github source 217 dirs — higher resolution rate than redhat due to OSS origins) → **1.4 GB**.
- **Not included in auto-update** (operational decision 2026-07-11): community catalog digests change frequently, which would trigger excessive rebuilds. Build manually as needed.
- Community containers are spread across third-party registries (ghcr.io, personal quay repos, etc.), and dead endpoints can cause `oc image info` to hang for minutes, so `phase-b-fetch-source.sh` label queries use `timeout 60`.

#### Swapping to production mounts (when new operator caskets are rebuilt)

```bash
sudo "$CASKET_WORK"/scripts/swap-operators-v2.sh   # fstab rewrite + daemon-reload + restart
```

If `systemctl restart` fails due to a busy mount, follow up with `umount -l` on that mount followed by `systemctl start` (occurred with 4.20 in practice). Edit the `VERS=` and date glob in the script to match the target filenames.

### Work directory layout

```
$CASKET_WORK/
├── README.md                      ← Overview and documentation index
├── CLAUDE.md  USAGE.md            ← Developer guide / user guide
├── scripts/                       ← Pipeline core (Phase A / A-rpm / B / B-operand) + lib-resolve.sh
├── tests/                         ← Network-free regression tests (run in CI)
├── .github/workflows/ci.yml       ← Lint + tests (the pipeline itself is not CI-tested)
├── docs/                          ← Design & planning documents
├── mcp/  opengrok/                ← MCP server / OpenGrok source browser
├── ocp<VERSION>/                  ← Phase A per-version intermediates (.gitignore)
│   ├── 00-discover/               release.json, release-commits.txt, images.tsv, commits.tsv
│   ├── 10-git/                    tarballs + fetch.log
│   ├── 40-manifest/               MANIFEST.json
│   └── 50-out/stage/              mksquashfs input tree
├── phase-a-rpm/                   ← A-rpm intermediates
│   ├── vm/                        cloud-init + virt-install bootstrap, in-VM scripts
│   ├── srpms/                     843 *.src.rpm (rsynced from VM)
│   ├── rpmdb/                     rpmdb tsv for 7 versions
│   ├── logs/                      wishlist / missing / fetch logs
│   └── 50-out/stage/              mksquashfs input tree
├── phase-b/<minor>/               ← Phase B intermediates (per minor)
│   ├── 00-discover/               configs/ (FBC), operators.tsv, bundles.tsv, containers.tsv, labels.tsv, git.tsv, containers-v2.tsv, git-v2.tsv
│   ├── 10-bundles/<op>/<head>/    extracted bundle: manifests/, metadata/
│   ├── 20-git/                    github tarballs (v1 + v2 appended, fetch.log + fetch-v2.log)
│   └── 50-out/stage/              mksquashfs input tree
└── phase-b-operand/<pkg>/<minor>/ ← B-operand intermediates (per product × minor)
    ├── 00-discover/               head bundle, images.tsv (operand list)
    ├── 20-git/                    operand github tarballs + fetch logs
    ├── 30-vendor/<comp>/          extracted Rust dependency sources (Cargo.lock resolved, target products only)
    └── 50-out/stage/              pre-integration stage (phase-b-operand-combine.sh consolidates)
```

Intermediates (`ocp<VERSION>/`, `phase-a-rpm/srpms`, `phase-a-rpm/50-out`) can be deleted after the final `.sqfs.xz` is generated. `phase-a-rpm/rpmdb/` and `phase-a-rpm/logs/` are worth keeping for diffing on the next update.
