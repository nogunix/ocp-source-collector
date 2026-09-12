# Casket improvement notes

Origin: Issues and improvement proposals found during the 2026-06-08 OCP-V New→Old VM migration investigation (analysis notes are private).

## Status (2026-06-08)

- **#1 / #3 → Implemented and deployed to production.** Created
  `scripts/build-source-index.py` and integrated it into Phase A/C/D package scripts
  (runs immediately after staging). Under each `git/`, it **generates**
  `INDEX.tsv` (dir|repo|ref|version|components) + `by-component/<name>` and
  `by-repo/<repo>` relative symlink trees (directory names unchanged = reversible).
  Retrofitting existing caskets uses `scripts/repackage-add-index.sh` (overlayfs to
  layer the index on top, then re-mksquashfs) + `scripts/swap-source-index.sh`
  (fstab replacement + mount restart; dry-run by default, `--apply` to execute).
  All 24 caskets were rebuilt as `casket-20260608-*` and swapped into production on
  2026-06-08 (old files retained for rollback).
  Adopted improvements: **option 2 (by-component/by-repo trees) + option 3 (INDEX.tsv)**
  from below. Option 1 (rename dirs to repo basename) was rejected as destructive;
  directory names left unchanged.
- **#2 → Partially addressed.** Documented in Phase D README that `method=b:component-map`
  with empty ref means "fetched by tag (not NO_SOURCE)". Adding the actual fetched tag
  column to git.tsv is not yet done.

Below is the original problem description (preserved for historical context).

## 1. [Most important] Deduped directory names don't represent the actual repo

### Symptom
When deduplicating by `(repo, tag)` or `(repo, commit)`, the generated directory name
is **the component name first seen for that key** (common behavior across Phase A/C/D).
This causes names to diverge from actual content in Phase D's `component-map "tag"`
mode, where multiple unrelated components map to the same `kubevirt/kubevirt` repo.

Example (CNV):

| Mount directory name | Actual content | Contents |
|---|---|---|
| `cnv/git/passt-network-binding-plugin-cni-v1.4.1/` (4.18) | **kubevirt/kubevirt @ v1.4.1 (full)** | `pkg/virt-config/`, `feature-gates.go`, `virt-launcher`, etc. |
| `cnv/git/libguestfs-tools-v1.6.5/` (4.20) | **kubevirt/kubevirt @ v1.6.5 (full)** | Same as above |

→ virt-api/virt-controller/virt-handler/virt-launcher/virt-operator/passt/libguestfs-tools
  are all consolidated into this one directory (dedup itself is correct).
→ **The most important source (KubeVirt core) is "hidden" under an unrelated name,
  making it undiscoverable via source browsing.** During this investigation, it was
  initially misidentified as "NO_SOURCE / uncollected".

### Impact
- Users (customers, support) cannot find the target source.
- Indexers like OpenGrok also display incorrect project names.

### Improvement options (any one of)
1. **Rename dedup directories to repo basename** — e.g., `kubevirt-v1.4.1/`,
   `containerized-data-importer-1.61.5/`. For commits: `kubevirt-<short_sha>/`. Most
   intuitive.
2. **Add component→dir symlink tree to mount** (same approach as Phase B's `by-ocp/`)
   — e.g., `cnv/by-component/virt-launcher -> ../git/kubevirt-v1.4.1`. Improves
   discoverability without changing existing directory names.
3. At minimum, place `git/README` or `git/INDEX.tsv` (component→actual dir→repo) under
   the mount root.

## 2. git.tsv tarball column shows unrelated name, empty ref looks like NO_SOURCE

### Symptom
In `meta/git.tsv`, kubevirt/kubevirt component rows show:

```
virt-api  https://github.com/kubevirt/kubevirt  (empty ref)  passt-network-binding-plugin-cni-v1.4.1.tar.gz  1.4.1  b:component-map
```

- tarball column = unrelated passt name (same root cause as #1).
- Empty ref + version only → easily misread as NO_SOURCE (actually fetched successfully
  via tag `v1.4.1`).

### Improvement options
- Add explicit columns for **actual fetched tag (v1.4.1) and actual DL URL** to git.tsv
  (traceability).
- Align tarball/dir column with #1's repo basename naming.
- Document in header/README that `method=b:component-map` with empty ref means "tag
  fetch" (currently distinct from `z:none`/tarball=`NO_SOURCE` but confusing).

## 3. No reverse lookup (dir → which operand image) on mount

Without reading `meta/MANIFEST.json`, it's impossible to tell which operand a given git
directory came from. A by-component symlink tree (#1 option 2) would resolve this. Also
beneficial for Phase A/C.

## 4. (Optional) Duplication between vendored and main source

`kubevirt.io/api` is vendored in ~19 operands, and kubevirt core is also collected.
squashfs xz dedup absorbs actual byte duplication so size impact is minimal, but noting
in README that "the canonical API schema is in the main source's
`staging/src/kubevirt.io/api`" would reduce confusion.

## 5. Phase B SRPM: patches unapplied, downstream features invisible in source tree

### Symptom
`srpms/<NEVR>/` contains spec + extracted source + `patches/`, but **patches are
unapplied**. RHEL-specific features are added via patches, so they are invisible when
browsing the source tree.

Example: machine type `pc-q35-rhel9.6.0` **does not exist** in
`qemu-kvm-9.1.0-15.el9_6.18/qemu-9.1.0/` — it only exists in
`patches/0025-redhat-Add-rhel9.6.0-machine-type.patch`.
(During the qemu machine type investigation, it was necessary to also grep `patches/`.)

### Impact
- Downstream-specific behavior (machine types, RHEL-specific CVE fixes, etc.) that
  users expect to "just see in the source" is missed. OpenGrok also indexes the
  unapplied state.

### Improvement options
- Document in `srpms/<NEVR>/README` or meta that "patches are unapplied; see `patches/`
  for downstream diffs". ✅
- (Optional, heavy) Provide a separate applied tree by running `%prep` equivalent to
  apply `patches/`.

### Applied-tree mode implementation (2026-06-08, code)
Added **`PHASE_B_APPLY=1`** mode to `scripts/phase-b-extract-one.sh` (off by default =
safe, preserving raw extraction behavior). When enabled: `rpm -i` the `.src.rpm` into
a temporary `_topdir` → `rpmbuild --nodeps -bp` to run %prep (%setup+%autopatch, etc.),
staging the **applied source tree** at `<NEVR>/<tree>` (`.patches-applied` marker;
`patches/` retained for reference). Falls back to raw extraction on %prep failure
(`.patches-unapplied`). Supports both rpm4 (`BUILD/<tree>`) and rpm6
(`BUILD/<n>-<v>-build/` + SPECPARTS) layouts. Verified with synthetic SRPM (patch
applied → reflected in tree, marker created). Propagated to `phase-b-package.sh` via env.

### Production deployment (2026-06-08, complete)
`rhel9-srpm` VM was **shut off, not destroyed** (`sudo virsh list --all` shows it,
qcow2 intact, SCA registered). After startup, confirmed that `~/scripts/srpms/`
contained **all 843 src.rpm** → no re-download needed. Procedure:
1. Start VM, SSH (cloud-user), rsync 843 src.rpm to host `phase-b/srpms/` (8.5G).
2. On host (rpm6): `PHASE_B_APPLY=1 phase-b-package.sh` (`KEEP_STAGE` reuse) →
   applied extraction. `%autopatch -p1` format: applied successfully (675), %prep
   failures (168, due to redhat-rpm-config macro/tool differences): raw fallback.
3. Removed `.git` directories (167, 8.6G) created by `%autosetup -S git`
   (permanently added to script) → 14G → **5.3G**.
4. Swapped `casket-20260608-ocp-srpms.sqfs.xz` (5.3G) (old 20260529 retained, fstab
   backup `/etc/fstab.bak-20260608-srpms-applied`).
5. Verified: `/srv/sources-ocp-srpms/srpms/qemu-kvm-9.1.0-15.el9_6.18/qemu-9.1.0/hw/i386/pc_q35.c:675`
   shows `pc_q35_rhel_machine_9_6_0_options` (RHEL-9.6.0 PC) **visible in source**
   (previously only in patches/).

> Note: applied 675 / unapplied 168. Unapplied are cases where %prep failed on the
> host (rpm6) — raw tree + `.patches-unapplied` marker (see patches/). Running
> extraction on the RHEL VM (rpm4) would improve success rate (key targets like qemu
> are already applied). OpenGrok's `srpms` project indexes old content → refresh
> requires re-indexing (next run-opengrok.sh).

## 6. Phase B SRPM by-ocp missing 4.21 (Phase A/C/D have 4.21)

`sources-ocp-srpms/by-ocp/` covers 4.14–**4.20 only**. Meanwhile layered/Phase A/C
provide up to 4.21. → SRPM/qemu cross-referencing for 4.21 (e.g., checking 4.21 qemu
machine types) is **not possible**. Caused by Phase B 4.21 collection being PAUSED
([[phase-b-421-progress]]). Adding 4.21 by resuming collection resolves this.

## 7. OpenGrok: `chown -R` on 215G index volume at every startup (~20 min)

### Symptom
`opengrok/scripts/entrypoint-ro.sh` runs `chown -R appuser:appgroup /opengrok/data`
on **every startup**. The index volume is ~215G, blocking tomcat/REST availability for
**~20 minutes each time** (confirmed via podman top: PID4 chown running for several
minutes, REST returning 401/no response).

### Fix (implemented + verified 2026-06-08)
Made idempotent: skip -R if already owned by `appuser`. Only runs on first creation
(root-owned by podman) or airgap deployment (root-extracted). Normal restarts skip
immediately. `FORCE_DATA_CHOWN=1` forces it.
→ Verified: `podman restart` shows "already owned by appuser; skipping recursive chown",
no chown process, **REST responds in seconds** (was ~20 min). `def` search returns
results immediately.

## Priority / implementation status

| # | Improvement | Priority | Scope | Status |
|---|---|---|---|---|
| 1 | Dedup directory discoverability (by-component/by-repo symlink trees) | **High** | Phase A/C/D | ✅ Implemented + **deployed to production** (casket-20260608-*, 24 caskets live 2026-06-08) |
| 2 | git.tsv component-map=tag documentation (prevent NO_SOURCE misread) | Medium | Phase D | ✅ README note added |
| 3 | Reverse lookup index on mount (git/INDEX.tsv) | Medium | Phase A/C/D | ✅ Implemented + deployed to production |
| 4 | Vendored vs main source note | Low | Phase D (CNV) | ⬜ Not done |
| 5 | SRPM patches unapplied → applied-tree mode (PHASE_B_APPLY=1) | Medium | Phase B | ✅ **Deployed to production** (casket-20260608-ocp-srpms 5.3G live; applied 675/unapplied 168; pc-q35-rhel9.6.0 visible) |
| 6 | SRPM by-ocp missing 4.21 (resume Phase B) | Medium | Phase B | ✅ Resolved (`by-ocp/` now covers 4.14–4.22, all 9 minors) |
| 7 | OpenGrok startup chown -R (~20 min) made idempotent | Medium | OpenGrok | ✅ Implemented + production-verified (~20 min → seconds) |

### Implementation details (2026-06-08)
- New `scripts/build-source-index.py`: From staged `meta/MANIFEST.json` (auto-detects
  A/C/D formats), generates `git/INDEX.tsv` (dir|repo|ref|version|components) and
  `by-component/<name>` / `by-repo/<repo name>` relative symlink trees. Makes entities
  hidden by dedup naming discoverable (e.g., `by-repo/kubevirt` → core tree,
  `by-component/virt-launcher` → same).
- `scripts/package.sh` (A) / `phase-c-package.sh` (now `phase-b-package.sh`) /
  `phase-d-package.sh` (now `phase-b-operand-package.sh`) call the above after staging
  + add README note.
- `scripts/phase-b-package.sh`: Added patches-unapplied note to README (#5).
- Existing git/ directory names are **not changed** (additions only, reversible).
  squashfs preserves relative symlinks, which resolve after mounting.
- Applying changes to existing caskets requires repackaging (rebuild).
  #4 is not yet addressed.

### Production deployment (2026-06-08)
Intermediate work was already deleted, so to avoid full re-fetch,
`scripts/repackage-add-index.sh` was used to regenerate via **overlayfs** (read-only
casket mount + generated index layered on top → re-mksquashfs, reusing actual source
content). 24 caskets rebuilt as `casket-20260608-*.sqfs.xz` (`scratch/rebuild-all.sh`,
~80 min, all OK). `scripts/swap-source-index.sh --apply` replaced fstab + restarted
mount units (old versions retained for rollback,
`/etc/fstab.bak-20260608-preindex`). All 24 mounts are live with indexes.
Verification example: `/srv/sources-layered-ocp4.20/cnv/by-repo/kubevirt` → resolves to
kubevirt core (`pkg/virt-config`).

> Note: #1 stems from the existing design where "dedup uses the first-seen component
> for filename" (documented in `CLAUDE.md` architecture notes). Resolved by adding a
> discoverability layer without changing directory names. #1–#4 were found during the
> 2026-06-08 New→Old VM migration investigation; #5–#6 emerged during qemu machine
> type verification in that same investigation.
