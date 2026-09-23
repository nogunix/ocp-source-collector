# A-rpm collection procedure (node OS + extensions + EUS backfill + in-container RPM layer)

The contents of `casket-*-ocp-srpms.sqfs.xz` are **the confluence of several independent collection streams**. The packaging scripts only read the result: the `*.src.rpm` files plus the per-patch rpmdb tsvs. The merge itself is a human step. It first went wrong on 2026-08-11, when a naive build dropped from 3270 packages to 795, and again on 2026-09-07 (see [Traps](#traps)). This document exists to prevent both.

> **Important**: The casket only holds extracted source trees and does not retain `.src.rpm` files (by design). This means **collected artifacts cannot be recovered from the mount**. Always verify that a `srpms*/` directory is not the only copy before deleting it (`scripts/backup-srpm-corpus.sh` backs them up).

---

## Overview

```
                                        ┌─────────────────────────────┐
 A.  Node OS base (rhel-coreos)      ─▶ │ phase-a-rpm-collect.sh      │
 B.  EUS/E4S/FDP backfill            ─▶ │   (container, one pass)     │─▶ srpms-<date>/ + rpmdb-<date>/
 A2. Node OS extensions (…-ext.)     ─▶ │   reads *-extensions.tsv    │        │
                                        └─────────────────────────────┘        ▼
 C.  In-container RPM layer          ─▶  inventory only (see C)          repackage-srpms-refresh.sh
                                                                          (overlay onto the live casket)
```

| Stream | What | Tooling (2026-09) |
|---|---|---|
| A. Node OS base | `rhel-coreos` rpmdb of each tracked patch | `scripts/phase-a-rpm-collect.sh` (container) |
| A2. Node OS extensions | `rhel-coreos-extensions` RPMs (kata, kernel-rt, usbguard, …) | `scripts/phase-a-rpm-extensions-inventory.sh`, then fed to the same container run |
| B. EUS/E4S/FDP backfill | micro-releases (`el9_N`, `el9fdp`) that the plain repos lack | inside the container run: pinned `dnf`, then `cdn-fetch.py` |
| C. In-container RPM layer | RPMs inside product container images (qemu in virt-launcher, ceph in ODF, …) | inventory: `scripts/phase-a-rpm-image-inventory.py`. Fetching: see C |

Live casket as of 2026-09-07: **3485 SRPMs / 18G**.

**The node OS is not "1 image = 1 rpmdb".** The payload has `rhel-coreos-extensions` separate from `rhel-coreos` (base), and from 4.21 onward two more el10 images are added. Collecting only stream A drops kata and others entirely. See A2.

---

## Standard procedure: incremental refresh (container)

This is the normal path when upstream patches move (`casket-check.sh --phase a-rpm` reports STALE). It adds only the new packages to the live casket.

### Prerequisites

- `podman`, and a pull secret at `~/.docker/config.json` (or `$AUTHFILE`).
- **Entitlement**, one of:
  - `~/.config/casket/rhsm.env` (chmod 600) containing `RHSM_ORG=` + `RHSM_ACTIVATION_KEY=` (preferred) or `RHSM_USERNAME=` + `RHSM_PASSWORD=`. The container registers itself and unregisters on exit. **This is the path on casket-host**, which is Fedora: registering the *host* yields an empty `redhat.repo` (no RHEL product certificate), whereas UBI9 carries one.
  - A RHEL host's own `/etc/pki/entitlement`, mounted automatically when present.

  Without either, the script dies up front. It deliberately does not "succeed" with an empty `srpms/`.
- Run from **bash**, never zsh (see [Traps](#traps)).

### Steps

```bash
cd ~/ocp-source-collector
STAMP=$(date -u +%Y%m%d)

# 1. A2 inventory: current patch of every minor in config/phase-a-rpm-minors.txt
scripts/phase-a-rpm-extensions-inventory.sh -o phase-a-rpm/rpmdb-extensions-$STAMP

# 2. Put the extension tsvs where the container run will read them
mkdir -p phase-a-rpm/rpmdb-$STAMP
cp phase-a-rpm/rpmdb-extensions-$STAMP/*-extensions.tsv phase-a-rpm/rpmdb-$STAMP/

# 3. A + B (and the A2 packages from step 2): rpmdb extraction + SRPM fetch
CASKET_COLLECT_STAMP=$STAMP scripts/phase-a-rpm-collect.sh
#   builds casket-srpm-collect:el9 on first use (--build-image to rebuild)
#   versions come from config/phase-a-rpm-minors.txt (--versions "4.20.35 …" to override)
```

Step 3 writes `phase-a-rpm/srpms-$STAMP/` and `phase-a-rpm/rpmdb-$STAMP/`. **It never writes to `srpms/`**, which is the merge point (see [From scratch](#from-scratch-full-rebuild)). Inside the container, `collect.sh` does the following:

1. Extracts the `rhel-coreos` rpmdb for each patch into `rpmdb-<date>/<patch>.tsv`. A tsv that already exists is kept.
2. Builds the wishlist from **every** `*.tsv` in that directory. That is why step 2 works: the `-extensions` tsvs are picked up. The rhocp repo minors come from both the filenames and the `rhaos<M>.<m>` dist tags, so `kata-containers-…rhaos4.19` in a 4.20 payload still resolves.
3. Fetches the SRPMs with `dnf download --source` in batches.
4. Retries the micro-releases with pinned `--releasever` against EUS/E4S/HA/RS/RT/CRB/FDP.
5. Hands the remainder to `cdn-fetch.py`. It reads each repo's own metadata once per needed releasever and downloads straight from the CDN with the entitlement cert. It also reads the releasever off each package's dist tag, so el10 or a future 9.10 need no edit.

Results land in `phase-a-rpm/`: `fetch.log`, `missing.txt` (still unresolved) and `cdn-unresolved.txt` (genuinely gone from the CDN).

```bash
# 4. Stream C: re-run the inventory (products get added to b / b-operand over time)
CASKET_WORK=$PWD scripts/phase-a-rpm-image-inventory.py
wc -l phase-a-rpm/image-rpms-missing.txt     # 0 = nothing to fetch for C (as of 2026-09-13)

# 5. Overlay the new packages onto the live casket
scripts/repackage-srpms-refresh.sh -m /srv/sources-ocp-srpms \
  -s $PWD/phase-a-rpm/srpms-$STAMP -r $PWD/phase-a-rpm/rpmdb-$STAMP \
  -o /mnt/hdd/casket-ocp/casket-$STAMP-ocp-srpms.sqfs.xz
```

Step 5 expands only the NEVRs that are not already in the casket. `-r` **replaces** `meta/rpmdb/`, `by-ocp/` and `meta/bin-to-src.tsv` with exactly what that directory contains, so it must hold both the base and the `-extensions` tsvs.

```bash
# 6. Verify, then register and swap
ls /mnt/…/by-ocp/          # (mount the new .sqfs.xz) expect <patch> AND <patch>-extensions for every patch
CASKET_WORK=$PWD bash -c 'source scripts/lib.sh; source scripts/lib-fingerprint.sh
  python3 "$REGISTRY_PY" add --phase a-rpm --unit all \
    --fingerprint "$(phase_a_rpm_fingerprint)" \
    --artifact-path /mnt/hdd/casket-ocp/casket-'$STAMP'-ocp-srpms.sqfs.xz \
    --mount-path "$(mount_path_for a-rpm all)"'
sudo scripts/casket-swap.sh --phase a-rpm --unit all --apply
```

Before registering, **verify that the casket's `by-ocp/` patch set matches the current upstream**. The fingerprint is computed from upstream, so if a z-stream has moved since extraction, the contents and the fingerprint will disagree.

Measured on 2026-09-07 (9 patches, base only): 984 SRPMs fetched, about 4.5 hours. Of those, `cdn-fetch.py` took about 10 minutes for the EUS/E4S remainder, and 351 were left in `cdn-unresolved.txt`. The overlay expanded 95 new NEVRs and mksquashfs took about 2h20m, giving 3485 SRPMs / 18G.

---

## Traps

- **`-r` must include the extension tsvs, or the extension trees vanish.** On 2026-09-07 the refresh was run with an `rpmdb-20260907/` holding only the 9 base tsvs. The live casket lost every `by-ocp/<patch>-extensions/` tree and the extension rows of `meta/rpmdb/` / `bin-to-src.tsv`. The extension SRPMs themselves survived in `srpms/`, because the overlay keeps existing NEVRs. Nothing failed or warned. Step 2 above exists because of this. Always check `by-ocp/` for the `-extensions` trees before swapping.
- **Do not source `lib.sh` from zsh.** `${BASH_SOURCE[0]}` does not resolve, so `CASKET_WORK` becomes `$HOME`. `phase_a_rpm_fingerprint` then returns the sha256 of the empty string (`01ba4719…`), with only one `No such file` warning. Registering that value is a silent corruption. Always use `CASKET_WORK=$PWD bash -c '…'`.
- **Keep every stream's output in dated directories.** `srpms/` is the merge point for a from-scratch build. Writing one stream straight into it leaves a partial corpus that `phase-a-rpm-package.sh` will happily package (3270 → 795).
- **The rhsm.env mount needs SELinux relabelling (`,z`).** `~/.config` is `user_home_t`. Without the relabel the bind mount "works" but the file reads as absent, and the run reports "no entitlement" for a file that is right there. `phase-a-rpm-collect.sh` already does this; keep it if you edit the mount.
- **`--rpmdb-image=rhel-coreos` is mandatory.** 4.14/4.15 still carry the old el8 `machine-os-content`, and the default picks that one. `collect.sh` passes it; keep it.
- **Don't expose auxiliary files to `*.tsv` globs.** `collect.sh`, `phase-a-rpm-by-ocp.sh` and the legacy `02` read their input as a bare `"$DIR"/*.tsv`. That is why the extension inventory keeps its 3-column `extensions-map.tsv` under `meta/`. Copy only the `*-extensions.tsv` files into `rpmdb-<date>/`.
- **rhaos dist tags don't match the payload's minor.** 4.20.32's extensions carry `kata-containers-3.31.0-4.rhaos4.19.el9`, whose SRPM lives in the `rhocp-4.19` source repo. `collect.sh` derives the rhocp minors from the content as well as the filenames, so this resolves. A tool that only looks at filenames misses these packages silently.
- **The extension inventory and the collect run each resolve the current patches.** Run them back to back. If a z-stream lands in between, the base and the `-extensions` tsvs describe different patches. Pin both with `-v` / `--versions` if in doubt.

---

## A2. Node OS extensions (`rhel-coreos-extensions`)

The base rpmdb is **only half of the node OS**. Optional features ship as plain RPMs in the separate payload image `rhel-coreos-extensions`:

```
MachineConfig{Spec:{Extensions: ["sandboxed-containers"]}}
  → machine-config-operator translates to "kata-containers"
  → rpm-ostree installs on the node
```

**These never appear in the base rpmdb.** In 4.20.32: 162 RPMs / 10 extensions, of which 89 binaries (23 SRPMs) were absent from the entire casket when this was found (2026-08-12). Found via kata: the OSC operator resolves this exact image at runtime in `sandboxed-containers-operator/controllers/daemonset_reconcile.go:34`. The gap is not only kata: `kernel-rt`, `usbguard`, `libreswan`, `pacemaker`/`pcs`, `crun-wasm`, `wasmedge`, `fence-agents` and others are affected too.

`scripts/phase-a-rpm-extensions-inventory.sh` extracts `/usr/share/rpm-ostree/extensions/` with `oc image extract` and runs `rpm -qp`. It needs no subscription and installs nothing. Output under `phase-a-rpm/rpmdb-extensions-<date>/` (or `-o`):

| File | Contents |
|---|---|
| `<patch>-extensions.tsv` | name\|epoch\|version\|release\|arch (same format as the base tsvs) |
| `meta/extensions-map.tsv` | patch\|extension\|package |
| `meta/extensions-missing.txt` | SRPMs not in the pool = what the collect run must fetch |
| `meta/extensions-deferred.txt` | payload tags intentionally not collected (el10, below) |
| `cache/<digest>/` | extracted RPMs (re-runs take seconds) |

On the mount these appear as `by-ocp/<patch>-extensions/`, deliberately separate from the default-installed set.

---

## C. In-container RPM layer

The RPM layer **inside container images**, outside the node OS: qemu/libvirt in virt-launcher, ceph in ODF, and so on. This is the largest stream (2203 of the 3315 SRPMs in 2026-08).

### C-1. Inventory

```bash
CASKET_WORK=$PWD scripts/phase-a-rpm-image-inventory.py [--limit N] [--pool DIR]
```

Uses the Pyxis API (`catalog.redhat.com`, **no image pull or authentication needed**) to fetch each image's RPM manifest. By default it diffs against `/srv/sources-ocp-srpms/srpms`. Output under `phase-a-rpm/`:

| File | Contents |
|---|---|
| `pyxis-cache/<digest>.json` | raw responses (re-runs are free) |
| `image-rpms.tsv` | digest \| repo \| srpm_nevr |
| `image-rpms-missing.txt` | **SRPM NEVRs not in the pool = what needs to be fetched** |
| `image-rpms-unresolved.tsv` | digest resolution failures (with reasons) |

Digest lookup order: `manifest_list_digest` → `image_id` → `manifest_schema2_digest`.

**The input is the `images.tsv` from b / b-operand, so re-run the inventory whenever products are added.** Otherwise the scope does not grow. On 2026-08-12, osc had been added to b-operand after the last inventory, so all 8 of its images were missing from `image-rpms.tsv`.

### C-2. Fetching — **no verified path**

The 2203 SRPMs in `srpms-containers/` were fetched by hand in 2026-07, and the commands were not recorded. As of the 2026-09-13 inventory, `image-rpms-missing.txt` is **empty**, so nothing is outstanding. The next product addition can change that.

The likely path is `containers/srpm-collect/cdn-fetch.py`, which already does "CDN direct" for stream B. It takes a missing-NVR list and resolves each NVR against the repo metadata in `redhat.repo`. **Not verified for stream C:**

- the NVR format of `image-rpms-missing.txt` against `--missing`
- whether the UBI9-registered container gets el8 / layered-product repo definitions at all (`srpms-containers/` holds el8 packages such as `acl-2.2.53-1.el8`)

`dnf download --source` is no substitute here, because it rejects the src arch for these lookups. The original mechanism was `repoquery --arch=src --location` plus `curl --cert <entitlement>`.

---

## From scratch (full rebuild)

Only needed if the live casket is lost or its layout changes. Collect every stream into dated directories, back them up (`scripts/backup-srpm-corpus.sh`), then merge by hand:

- `phase-a-rpm/srpms/`: the union of every `srpms-<date>/`, `srpms-ext-*/`, `srpms-fill*/` and `srpms-containers/`
- `phase-a-rpm/rpmdb/`: the current patches' base **and** `-extensions` tsvs

Then run:

```bash
scripts/phase-a-rpm-package.sh -o /mnt/hdd/casket-ocp
```

It dies unless **both** `srpms/` and `rpmdb/` are present. Suffixed directories such as `rpmdb-<date>/` are not read; symlink or rename them. Without C-2 (above), a from-scratch build depends on the existing `srpms-containers/` copy.

---

## Legacy: VM procedure (superseded 2026-08)

Before `phase-a-rpm-collect.sh`, streams A and B ran on the libvirt VM `rhel9-srpm` with `phase-a-rpm/vm/scripts/01-extract-rpmdb.sh` → `02-fetch-srpms.sh` → `03-fetch-eus.sh` → `03c.sh`. The container run replaces all of them in one pass and is faster: about 4.5h against 6h+ in 2026-09. The VM path had its own per-spec EUS retry loop, which took about 4.5 hours on its own. **Do not use it for normal refreshes.** If you must:

- Output to a fresh `~/run-<date>/`. The VM keeps leftovers from interrupted runs (`~/srpms`, `~/rpmdb-tsv`) that would otherwise mix in.
- Give `01` the current patches through `VERSIONS_OVERRIDE`. Its built-in default is a stale hardcoded list.
- `03-fetch-eus.sh` writes to `~/scripts/srpms` unless `OUT_DIR` is set. `03c.sh` hardcodes `cd ~/scripts`, `--destdir srpms` and `missing.txt`, and cannot be redirected.
- `02` disables EUS/E4S at startup (an unpinned releasever returns 404 and kills it under `set -e`); `03` re-fetches with pins. Keep the order.
- rpmdb extraction for 4.14–4.18 needed an older rpm than the Fedora host has. The container image has the right one.

---

## Remaining work

1. **C-2 (stream C fetching) has no verified script.** See C-2. It is not urgent while `image-rpms-missing.txt` stays empty, but a from-scratch rebuild still depends on the hand-collected `srpms-containers/`.
2. **el10 node OS is uncollected (4.21/4.22). It is opt-in, so priority is low.** The payload has `rhel-coreos-10` and `rhel-coreos-10-extensions`, both real RHCOS on RHEL 10.2 (`com.coreos.osname=rhcos`). **Neither is the default:** MCO's `pkg/osimagestream/streams.go` (`GetBuiltinDefaultStreamName`) returns `rhel-9` whenever `releaseVersion.Major() == 4`, so el10 only matters for clusters that override `OSImageStream`'s `spec.default`. It becomes the default in OCP 5. (`oc adm release info`'s `displayVersions.machine-os` reads 10.2 for 4.21/4.22, but that does not indicate the default stream.)
   - Read side: `collect.sh` hardcodes `--rpmdb-image=rhel-coreos`, and the extension inventory's `TAG_COLLECT` matches el9 only. Deferred tags are written to `meta/extensions-deferred.txt` on every run, so the gap stays visible. The Fedora host's rpm reads the el10 rpmdb fine (489 packages measured).
   - Fetch side: `cdn-fetch.py` already derives the major from the dist tag, but the container is registered as RHEL 9 and has no `rhel-10-*` repo definitions. Getting those is the real work.
3. `phase-a-rpm-package.sh` requires a fixed `rpmdb/` name and has no equivalent of `-r`.
