# A-rpm collection procedure (node OS + extensions + EUS backfill + in-container RPM layer)

The contents of `casket-*-ocp-srpms.sqfs.xz` are **the confluence of 3 different collection streams**, and `phase-a-rpm-package.sh` simply reads the result (the `*.src.rpm` files placed in `phase-a-rpm/srpms/`). The confluence procedure itself was never documented, and the problem surfaced during the 2026-08-11 update in the form of "a naive build drops from 3270 → 795". This document prevents that recurrence.

> **Important**: The casket only holds extracted source trees and does not retain `.src.rpm` files (by design). This means **collected artifacts cannot be recovered from the mount**. Always verify that `srpms/` is not the only copy before deleting it.

---

## Overview

```
                                     ┌──────────────────────────┐
 A.  Node OS base (rhel-coreos)    ─▶│                          │
 A2. Node OS extensions (…-ext.)   ─▶│  phase-a-rpm/srpms/      │─▶ phase-a-rpm-package.sh
 B.  EUS/E4S/FDP backfill          ─▶│  *.src.rpm consolidated  │    └─ 50-out/stage → .sqfs.xz
 C.  In-container RPM layer (CDN)  ─▶│                          │
                                     └──────────────────────────┘
```

Scale as of 2026-08:

| Stream | Count | Automation |
|---|---|---|
| A. Node OS base | 795 | **Done** (`01`/`02`) |
| A2. Node OS extensions | 77 SRPMs uncollected (discovered 2026-08-12) | Inventory **done**, fetching awaits VM execution |
| B. EUS backfill | ~120 (`srpms-fill*` 33+45+45) | **Done** (`03`) |
| C. In-container RPM layer | 2203 (`srpms-containers/`) | **Inventory only**. Fetching is manual |
| Post-confluence casket | **3315** | — |

**The node OS is not "1 image = 1 rpmdb".** The payload has `rhel-coreos-extensions` separate from `rhel-coreos` (base), and from 4.21 onward two more el10 versions are added. Collecting only stream A drops kata and others entirely. See A2 for details.

---

## A. Node OS (inside RHEL9 VM)

VM `rhel9-srpm` (libvirt, autostart disabled, registered and persisted).

```bash
sudo virsh start rhel9-srpm
sudo virsh net-dhcp-leases default          # get IP
scp ~/.docker/config.json cloud-user@$VMIP:~/pull-secret.json
scp phase-a-rpm/vm/scripts/*.sh cloud-user@$VMIP:~/scripts/

ssh cloud-user@$VMIP
cd ~/scripts
VERSIONS_OVERRIDE="4.14.58 4.15.59 …"  \
  CACHE_DIR=$HOME/run-<date>/rpmdb-cache \
  TSV_DIR=$HOME/run-<date>/rpmdb-tsv \
  ./01-extract-rpmdb.sh                     # ~10 min for 9 versions

TSV_DIR=$HOME/run-<date>/rpmdb-tsv \
  OUT_DIR=$HOME/run-<date>/srpms \
  WISH=$HOME/run-<date>/wishlist.txt \
  LOG=$HOME/run-<date>/fetch.log \
  MISSING=$HOME/run-<date>/missing.txt \
  ./02-fetch-srpms.sh                       # ~3.5 hours
```

**Always output to a new `run-<date>/` directory.** The VM retains leftovers from previous interrupted runs (`~/srpms` with 226 items, `~/rpmdb-tsv` with old patches), and mixing them would include rpmdb from non-existent patches in the casket.

`VERSIONS_OVERRIDE` **must receive current patches**. The default when omitted is a hardcoded stale value. To get current values:

```bash
CASKET_WORK=$PWD bash -c 'source scripts/lib.sh; source scripts/lib-fingerprint.sh
  while read -r m; do [ -n "$m" ] && fetch_patch "$m"; done \
    < <(grep -v "^#" config/phase-a-rpm-minors.txt | grep -v "^$")'
```

### Traps

- **rpmdb extraction requires the VM.** The Fedora host's newer rpm fails at rpmdb conversion for 4.14-4.18 due to format mismatch (`docs/design-notes.md:14`)
- **`--rpmdb-image=rhel-coreos` is mandatory.** 4.14/4.15 have the old el8 `machine-os-content` coexisting, and the default picks that one
- **Do not source `lib.sh` from `zsh`.** `${BASH_SOURCE[0]}` doesn't resolve, causing `CASKET_WORK` to become `$HOME`, which shifts both fingerprints and registry write targets

---

## A2. Node OS extensions (`rhel-coreos-extensions`)

`01` only reads the base rpmdb, which is **only half of the node OS**. Optional features are bundled as plain RPMs in the separate payload image `rhel-coreos-extensions`:

```
MachineConfig{Spec:{Extensions: ["sandboxed-containers"]}}
  → machine-config-operator translates to "kata-containers"
  → rpm-ostree installs on the node
```

**These never appear in the base rpmdb**, so they were invisible to a-rpm. In 4.20.32: 162 RPMs / 10 extensions, of which 89 binaries (23 SRPMs) were absent from the entire casket.

Discovered via kata (the OSC operator resolves this exact image at runtime in `sandboxed-containers-operator/controllers/daemonset_reconcile.go:34`), but the gap isn't kata alone: `kernel-rt`, `usbguard`, `libreswan`, `pacemaker`/`pcs`, `crun-wasm`, `wasmedge`, `fence-agents`, etc.

### Inventory (host-side, no VM needed)

```bash
scripts/phase-a-rpm-extensions-inventory.sh          # all minors, ~10 min
scripts/phase-a-rpm-extensions-inventory.sh -v 4.20.32   # single version
```

Extracts `/usr/share/rpm-ostree/extensions/` via `oc image extract` and runs `rpm -qp` to produce tsv in **the same 5-column format as `01`**. No VM or subscription needed (only reads RPMs, doesn't install them). Output goes to `phase-a-rpm/rpmdb-extensions-<date>/`:

| File | Contents |
|---|---|
| `<patch>-extensions.tsv` | name\|epoch\|version\|release\|arch |
| `meta/extensions-map.tsv` | patch\|extension\|package |
| `meta/extensions-missing.txt` | **SRPMs not in the pool = what needs to be fetched** |
| `meta/extensions-deferred.txt` | Intentionally uncollected payload tags |
| `cache/<digest>/` | Extracted RPMs (re-runs take 7 seconds) |

Measured 2026-08-12 (9 minors): 70/70/102/80/84/162/162/162/153 RPMs, **77** pool-missing SRPMs (kata alone accounts for 8 versions, one per minor).

### Fetching

Place the tsv files in the VM's `$TSV_DIR` and `02`/`03` will consume them. **They can be mixed into the same directory as `01` output** — the `-extensions` filename suffix is handled correctly in `02`'s minor derivation and `by-ocp` keying. On the mount, they appear as a separate tree at `by-ocp/<patch>-extensions/` (intentionally separated from the default-installed package set).

```bash
scp phase-a-rpm/rpmdb-extensions-<date>/*-extensions.tsv cloud-user@$VMIP:~/run-<date>/rpmdb-tsv/
# then same as A: 02 → B's 03
```

### Traps

- **Don't expose auxiliary files to `*.tsv` globs.** Both `02` and `phase-a-rpm-by-ocp.sh` read input via bare `"$DIR"/*.tsv` globs. A 3-column `extensions-map.tsv` placed alongside the 5-column package listings gets silently parsed as a package list, sprouting `by-ocp/extensions-map/`. That's why auxiliary output is isolated under `meta/`
- **rhaos dist tags don't match the payload's minor.** 4.20.32's extensions carry `kata-containers-3.31.0-4.rhaos4.19.el9`, and the SRPM lives in the `rhocp-4.19` source repo. `02` now derives the rhocp minor from **the `rhaos<MAJ>.<MIN>` in the content**, not just the tsv filename (added 2026-08-12). Without this, the miss is silent
- **el9 and el10 are separate images.** See "Remaining work" below

## B. EUS / E4S / FDP backfill (same VM)

What remains in `02`'s `missing.txt` is almost entirely **micro-releases** (`el9_2` / `el9_4` / `el9_6` …). These aren't in the normal repos — **`--releasever` must match the tag to pull from EUS/E4S**. In the 2026-08-11 run, 471 of 477 unresolved items were `el9_N` and 6 were `el9fdp`, all covered by the procedure below.

```bash
cd ~/scripts
cp $HOME/run-<date>/missing.txt .    # 03 scripts read ~/scripts/missing.txt
./03-fetch-eus.sh                    # EUS + E4S + fast-datapath
./03c.sh                             # EUS-only retry for remaining misses
```

`03-fetch-eus.sh` maps `el9_2→9.2`, `el9_4→9.4`, `el9_6→9.6`, `el9_0→9.0`, `el9→9` and runs `dnf download --source --releasever=<rv>` for each. `el9fdp` tries fast-datapath repos across 9.2/9.4/9.6. Output goes to `--destdir srpms`, so **results accumulate in the same directory as stream A**.

> `02-fetch-srpms.sh` **disables** EUS/E4S at startup (without a releasever pin, the CDN returns 404 and kills the script under `set -e`). Stream B explicitly re-fetches with pins afterward. Do not reorder.

---

## C. In-container RPM layer — **not automated**

The RPM layer **inside container images**, outside the node OS. Covers qemu/libvirt in virt-launcher, ceph in ODF, etc. At 2203 items, this is the largest stream — **2203 of a-rpm's 3315 items**.

### C-1. Inventory (scripted)

```bash
CASKET_WORK=$PWD scripts/phase-a-rpm-image-inventory.py [--limit N] [--pool DIR]
```

Uses the Pyxis API (`catalog.redhat.com`, **no image pull or authentication needed**) to fetch each image's RPM manifest. Output under `phase-a-rpm/`:

| File | Contents |
|---|---|
| `pyxis-cache/<digest>.json` | Raw response (re-runs are free) |
| `image-rpms.tsv` | digest \| repo \| srpm_nevr |
| `image-rpms-missing.txt` | **SRPM NEVRs not in the pool = what needs to be fetched** |
| `image-rpms-unresolved.tsv` | Digest resolution failures (with reasons) |

Digest lookup order: `manifest_list_digest` → `image_id` → `manifest_schema2_digest` (verified 2026-07-17).

**The input is the `images.tsv` from b / b-operand directly, so the inventory must be re-run when products are added.** The scope doesn't expand otherwise. Example: osc was added to b-operand on 2026-07-16, but the last inventory run was before that, leaving `sandboxed-containers-operator/*/00-discover/images.tsv`'s 8 images as 8/8 missing from `image-rpms.tsv` (confirmed 2026-08-12). Not a code bug — an execution ordering issue, fixed by re-running.

### C-2. Fetching (**no script** — this is the remaining work)

Only a one-line mechanism description exists in `docs/collection-model.md:77`:

> **CDN direct**: `repoquery --arch=src --location` + `curl --cert <entitlement>` (el8/el9/el10/layered all streams. `dnf download` rejects src arch, so not usable)

- `dnf download --source` can't be used because it **rejects src arch**. Use `repoquery --location` to get the actual CDN URL, then `curl` to download directly
- Entitlement certificates are at `/etc/pki/entitlement/*.pem`
- Covers **all streams** including el8 / el10 / layered (fast-datapath etc.). `srpms-containers/` indeed contains el8 packages like `acl-2.2.53-1.el8.src.rpm`

**Scripting this procedure is the remaining work.** The 2203 items from 2026-07 were collected manually, and the specific command sequences were not recorded. Reconstruction from the above mechanism is possible but unverified.

---

## Confluence and packaging

After collecting `.src.rpm` files from streams A/B/C into `phase-a-rpm/srpms/`:

```bash
scripts/phase-a-rpm-package.sh -o /mnt/hdd/casket-ocp
```

Dies if **both** `srpms/` and `rpmdb/` (`<patch>.tsv` files) are not present. Suffixed directories like `rpmdb-<date>/` are not read — create a symlink or rename.

### For incremental updates, use overlay (recommended)

When a full re-collection is unnecessary, **overlaying just the delta on top of the mounted casket** is orders of magnitude cheaper. Add new items while preserving the existing 2520:

```bash
scripts/repackage-srpms-refresh.sh \
  -m /srv/sources-ocp-srpms \
  -s $PWD/phase-a-rpm/srpms-<date> \
  -r $PWD/phase-a-rpm/rpmdb-<date> \
  -o /mnt/hdd/casket-ocp/casket-<date>-ocp-srpms.sqfs.xz
```

Existing NEVRs are auto-skipped. Passing new rpmdb via `-r` regenerates `by-ocp/` and `meta/bin-to-src.tsv` with current patch composition, and replaces `meta/rpmdb/`. Measured 2026-08-11: expanded only 45 new items, resulting in 3315 SRPMs / 4983 binaries / 0 unresolved.

Registration and swap:

```bash
CASKET_WORK=$PWD bash -c 'source scripts/lib.sh; source scripts/lib-fingerprint.sh
  python3 "$REGISTRY_PY" add --phase a-rpm --unit all \
    --fingerprint "$(phase_a_rpm_fingerprint)" \
    --artifact-path <out.sqfs.xz> --mount-path "$(mount_path_for a-rpm all)"'
sudo scripts/casket-swap.sh --phase a-rpm --unit all --apply
```

Before registration, **verify that the casket's `by-ocp/` patch set matches the current upstream**. The fingerprint is computed from upstream, so if a z-stream has moved since extraction, the contents and fingerprint will be out of sync.

---

## Remaining work

0. **el10 node OS is uncollected (4.21/4.22). However, it's opt-in so priority is low.**
   The payload has `rhel-coreos-10` and `rhel-coreos-10-extensions`, labeled `com.coreos.osname=rhcos` / `coreos.build.manifest-list-tag=4.22-10.2-…-node-image` — same **real RHCOS (RHEL 10.2 version)**, just different names. But `01` is hardcoded to `--rpmdb-image=rhel-coreos` and A2 targets only el9 tags. 4.22.8's el10 extensions alone have 215 RPMs / 9 extensions (`wasm` is absent).

   **Not the default node OS.** MCO's `pkg/osimagestream/streams.go` (`GetBuiltinDefaultStreamName`) returns `rhel-9` when `releaseVersion.Major() == 4`, so **OCP 4.x including 4.21/4.22 defaults to el9**; el10 becomes default starting from OCP 5. el10 only affects clusters that explicitly override `OSImageStream`'s `spec.default`.
   `oc adm release info`'s `displayVersions.machine-os` returns 10.2 for 4.21/4.22, but **that is not an indicator of the default stream** (this was incorrectly used to conclude "default is el10" on 2026-08-12. Use the MCO source for determination).

   Intentional deferral decided 2026-08-12 to proceed with el9 only first. Each A2 run writes deferred tag names to `meta/extensions-deferred.txt`, so it won't be silently ignored.

   **This is not just "add a tag".** Read-side and fetch-side differ in difficulty:

   - **Read-side doesn't need RHEL 10** (measured 2026-08-12). The Fedora host's rpm 6.0.2 successfully runs `oc adm release info --rpmdb --rpmdb-image=rhel-coreos-10`, yielding 489 packages (kernel 6.12.0-211.39.1.el10_2 etc.). Stream A's "VM required" trap is about reading *old* rpmdb with a *new* rpm (4.14-4.18); el10 goes the other direction. Code changes are indeed just 2 spots (`01`'s multi-image `--rpmdb-image`, A2's `TAG_COLLECT`)
   - **Fetch-side is the real work.** `02`/`03`'s `dnf download --source` won't work. RHEL 9's subscription-manager-generated `redhat.repo` has no `rhel-10-*` repos, and `03`'s `--releasever` pin doesn't help (it only substitutes `$releasever` in existing URLs, which have the product literally in the path as `content/dist/rhel9/`)

   Two options. **(A)** New RHEL 10 VM running `02`/`03`. **(B)** Script the C-2 CDN direct fetch (item #1 below). Evidence favors B: all 282 el10 SRPMs in the casket came **entirely** from `srpms-containers/` = stream C, zero from the `dnf` path. CDN direct can pull el10 from the RHEL 9 VM. **el10 support and #1 reduce to the same work.**

   **Note: 4.14/4.15's `machine-os-content` (old el8) is a separate matter, intentionally excluded as before** (see stream A traps above)
1. **Scripting C-2 (CDN direct fetch)** — The largest stream's 2203-item fetch procedure has only a 1-line mechanism description. Until this is filled in, a-rpm cannot be rebuilt **from scratch**
2. `phase-a-rpm-package.sh` requires a fixed `rpmdb/` name (no `-r` option equivalent)
3. `01-extract-rpmdb.sh`'s `VERSIONS` default is a stale hardcoded value (worked around with `VERSIONS_OVERRIDE`, but the default itself should come from config)
