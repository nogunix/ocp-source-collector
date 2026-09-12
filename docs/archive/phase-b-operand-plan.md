# B-operand (formerly Phase D) — Layered-product operand source collection

> "Phase D" in this document refers to what was renamed to "B-operand" on 2026-07-11. Original names are preserved for historical accuracy.

Plan for a new phase to collect **operand sources** for OpenShift Platform Plus (OPP) /
Red Hat OpenShift AI (RHOAI) / OpenShift Virtualization (CNV).

Created: 2026-06-03 / Status: **PoC complete (CNV @ 4.20)**. 3 scripts implemented.

## PoC results (CNV @ 4.20, 2026-06-03)

Ran discover→fetch→package end-to-end with `kubevirt-hyperconverged` and verified.

| Metric | Value |
|------|---:|
| Operand images (CSV relatedImages) | 61 |
| Source resolved | **50 / 61 (82%)** |
| Tarballs fetched (after repo,ref dedup) | 27 / 27 OK, 0 fail |
| Artifact | `casket-20260603-cnv-ocp4.20.sqfs.xz` = **344 MB** (`/mnt/hdd/casket-ocp-test/`) |

Resolution method breakdown: `a:upstream-vcs` (upstream github URL+commit) 36,
`b:component-map` (kubevirt-core→`kubevirt/kubevirt` tag) 13, `c:oci-source` 1,
`z:none` (unresolved) 11.

**Key finding**: Layered products built with konflux have `org.opencontainers.image.source`
pointing to **internal gitlab.cee**. Phase C's "github only" logic gets zero hits.
The actual source is in `upstream-vcs-url`/`upstream-vcs-ref`/`upstream-version` labels,
which Phase D prioritizes (`scripts/phase-d-resolve-labels.py`). Core kubevirt
(virt-*, passt, pr-helper, etc.) also points upstream to gitlab, so these are rescued
via component name→`kubevirt/kubevirt` + `upstream-version` tag mapping.

11 unresolved = OCP base (`ose-coredns`/`ose-csi-*`/`ose-kube-rbac-proxy`, 6 total,
**duplicates of Phase A**) + `virtio-win`/`virt-artifacts-server` (truly no source) +
`hostpath-csi-driver`/`hostpath-provisioner`/`kubevirt-ipam-controller` (internal gitlab
only, tag unknown). CNV-specific actual gaps are effectively 5 items.

## Downstream / upstream provenance (per product, 2026-06-08 investigation)

"Is Phase D downstream or upstream?" **varies by product**. Actual org distribution from
each `meta/labels.tsv` resolution source (@4.20):

| Product | Resolution source org | Type |
|---|---|---|
| ACM | stolostron (47) | **downstream** (RH public org) |
| MCE | stolostron (23), openshift (13) | **downstream** (public) |
| ACS | stackrox (12) | RH-owned (public, downstream=upstream) |
| ODF | red-hat-storage (2) | **downstream** (public) |
| Quay | quay (1) | RH (public) |
| RHOAI | red-hat-data-services (68) | **downstream** (public) |
| **CNV** | **kubevirt (38=upstream)** | **upstream** (cannot be made downstream; see below) |

→ **6 of 7 products already fetch from Red Hat's public downstream orgs** (`c:source-label` =
`org.opencontainers.image.source`/`source-location` pointing to public github).
**Only CNV is upstream**.

### Why CNV alone is upstream / why downstream is not possible

CNV (virt-* core) actual label values:
```
org.opencontainers.image.source = https://<internal-gitlab>/openshift-virtualization/konflux-builds/v4-20/kubevirt
upstream-vcs-url                = https://<internal-gitlab>/openshift-virtualization/downstream/kubevirt
upstream-vcs-ref                = <downstream commit>   upstream-version = 1.6.5-66-g…(upstream+RH commits)
```
Downstream source lives in **Red Hat's internal GitLab `<internal-gitlab>`**, which
is **not even DNS-resolvable from the public network** (requires VPN/SSO, no public
mirror). The resolver's `gh()` drops non-github URLs and falls back to public upstream
`kubevirt/kubevirt` (tag). **In this environment (non-RH-internal), there is no path
to downstream CNV source.**
For RHEL-specific behavior (machine types, etc.), use Phase B (qemu, applied tree)
as the alternative.

> In environments with internal access, CNV can be made downstream by extending `gh()`
> to "allow gitlab.cee + token clone" (limited to that environment). The other 6 products
> work as downstream with the current setup.

### Implemented scripts

- `scripts/phase-d-discover.sh -p <pkg> -v <minor>` — FBC extraction→head bundle→CSV→`images.tsv`
- `scripts/phase-d-fetch-source.sh -p <pkg> -v <minor> [--jobs N] [--limit N]` — Label resolution→tarball fetch
- `scripts/phase-d-resolve-labels.py` — Upstream-priority resolver (`COMPONENT_MAP` for kubevirt-core)
- `scripts/phase-d-package.sh -p <pkg> -v <minor> -i <infix> [-o <dir>]` — Extract stage→mksquashfs xz

Gotcha: bash `IFS=$'\t' read` treats tabs as whitespace and **collapses empty fields**.
Lines with empty ref/version cause column shifts, so git.tsv generation and DL list use
awk + `-` sentinel (fixed).

## Delivery format (integrated + all-minor deployment)

7 products delivered as **1 integrated casket per minor**: `casket-<date>-layered-ocp<minor>.sqfs.xz`
mounted at `/srv/sources-layered-ocp<minor>/{cnv,acs,mce,acm,rhoai,odf,quay}/`
(bundle/git/meta under each product). The integrated stage aggregates each
`phase-d/<pkg>/<minor>/50-out/stage` via `cp -al` into one tree, then runs mksquashfs
(`phase-d-combine.sh`).

**Deployed to all 8 minors (4.14–4.21) as of 2026-06-04**. Each 0.8–1.1G. 4.20=20260603,
others=20260604. Old per-product 4.20 caskets moved to
`/mnt/hdd/casket-ocp-test/superseded-perproduct-20260603/`.

Build procedure (single minor):
```
for pkg in <7 packages>: phase-d-discover.sh -p $pkg -v $MINOR
                         phase-d-fetch-source.sh -p $pkg -v $MINOR --jobs 8
                         phase-d-package.sh -p $pkg -v $MINOR -i <infix> --stage-only
phase-d-combine.sh -v $MINOR -o /mnt/hdd/casket-ocp
```

### All-minor resolution counts (operands resolved / total)

| minor | cnv | acs | mce | acm | rhoai | odf | quay |
|------|---|---|---|---|---|---|---|
| 4.14 | 42/49 | 12/12 | 32/33 | 10/46* | 42/49 | 3/3 | 1/10 |
| 4.15 | 48/55 | 11/11 | 34/35 | 47/55 | 39/49 | 3/3 | 1/10 |
| 4.16 | 53/60 | 12/12 | 38/39 | 13/56* | 89/100 | 3/3 | 1/10 |
| 4.17 | 53/58 | 12/12 | 37/38 | 50/64 | 89/100 | 3/3 | 1/10 |
| 4.18 | 54/59 | 12/12 | 38/39 | 50/64 | 89/100 | 3/3 | 1/10 |
| 4.19 | 55/60 | 12/12 | 38/39 | 50/64 | 89/100 | 2/2 | 1/10 |
| 4.20 | 50/61 | 12/12 | 38/39 | 50/64 | 89/100 | 2/2 | 1/10 |
| 4.21 | 61/64 | 12/12 | 38/39 | 50/64 | 89/100 | 3/4 | 1/10 |

\* ACM resolution is lower for 4.14/4.16 only. Likely caused by label format
differences in older versions (`org.opencontainers.image.source` not set,
`url`/`source-location` differences). Needs further investigation (other minors
are stable at 47–50/64). DL failures common across all minors are `openshift/*` and
`rh-openjdk` (Phase A duplicates).

## Cross-product results (2026-06-03, @ 4.20)

Following CNV, collected 6 more products. Table shows per-product collection breakdown
(individual casket sizes before integration).

| Product | pkg | version | operand | resolved | git dirs | casket |
|------|------|------|---:|---:|---:|---:|
| CNV | kubevirt-hyperconverged | 4.20.15 | 61 | 50 | 27 | 344M |
| ACS | rhacs-operator | 4.10.3 | 12 | 12 | 4 | 60M |
| MCE | multicluster-engine | 2.11.1 | 39 | 38 | 29 | 155M |
| ACM | advanced-cluster-management | 2.16.1 | 64 | 50 | 43 | 151M |
| RHOAI | rhods-operator | 2.25.6 | 100 | 89 | 38 | 273M |
| ODF | odf-operator | 4.20.13 | 2 | 2 | 2 | 11M |
| Quay | quay-operator | 3.16.4 | 10 | 1 | 1 | 9.3M |

Example repos: ACS=stackrox/*, MCE=openshift/assisted-*/hive/hypershift/stolostron/*,
ACM=stolostron/* (policy/observability/search/governance/console),
RHOAI=red-hat-data-services/* (kserve, vllm, kubeflow, modelmesh, odh-dashboard,
trustyai, data-science-pipelines…) + opendatahub-io/notebooks.

**Source URL label keys differ per product**, requiring resolver extension (priority order):
`upstream-vcs-url` (CNV konflux) → COMPONENT_MAP (kubevirt-core) →
`org.opencontainers.image.source` (ACM/MCE) → `source-location` (ACS) → `url` (RHOAI)
→ tag fallback.

Download failures (MCE 2 / ACM 3 / RHOAI 18) are **all `openshift/*` or `rh-openjdk`**
= OCP payload base shared images whose downstream commits are not on public github.
These are **already collected in Phase A (`/srv/sources-ocp4.20.22`)**, so there is no
actual loss.

### ODF / Quay (COMPONENT_MAP support added, 2026-06-03)

ODF/Quay operands lack github source labels (`url`=docs/catalog.redhat.com), so
`COMPONENT_MAP` was extended to a `name -> (repo, mode)` structure:

| Product | Component | repo | mode | Result |
|------|------|------|------|---:|
| ODF | odf-rhel9-operator | red-hat-storage/odf-operator | upstream-commit | OK (12M) |
| ODF | odf-console | red-hat-storage/odf-console | upstream-commit | OK (1.4M) |
| Quay | quay | quay/quay | tag (v3.16.4) | OK (13M) |

- `upstream-commit` = fetch using `upstream-vcs-ref` commit (exists on github).
  `tag` = downstream commit not on public github, so fetch by version tag. Duplicate
  refs (comma-separated) use the first.
- ODF=2/2, Quay=1/10 resolved. `casket-20260603-{odf,quay}-ocp4.20.sqfs.xz` (11M/9.3M)
  mounted in production (`/srv/sources-{odf,quay}-ocp4.20`).
- **Quay's clair/quay-builder/quay-operator are structurally impossible**: RH internal
  build versions/commits don't match public upstream github (clair v4.x / quay-builder
  v3.2 / quay-operator v3.7).
- **ODF's actual storage sources** (ceph/rook/noobaa/ocs) are **confirmed out of scope
  (2026-06-03)**. Technically recoverable by collecting separate packages
  (`ocs-operator`/`rook-ceph-operator`/`cephcsi-operator`/`odf-csi-addons-operator`/
  `noobaa`) via Phase D, but ceph in particular is large and falls outside casket's
  target of "OpenShift platform code". ODF collection stops at the umbrella
  (odf-operator + odf-console).

## Plan (original design below, validated by CNV PoC)


## Naming convention (finalized 2026-06-03)

Repository name is `ocp-source-collector` (formerly `casket-ocp`, renamed 2026-09).
Internal variables (`CASKET_WORK`) and archive names (`casket-*`) refer to the casket
format and remain unchanged.
README/CLAUDE.md descriptions were expanded from "OCP release payload" to "OpenShift
ecosystem (OCP core + layered products)" when Phase D was started.

Filenames use product infix for distinction:
- OPP/CNV (tied to OCP minor): `casket-<date>-<product>-ocp<minor>.sqfs.xz` ("ocp<minor>" indicates the corresponding OCP version)
- RHOAI (version-independent): `casket-<date>-rhoai-<2.x>.sqfs.xz` (no "ocp" prefix)

## Background and problem

Phase C (now Phase B) scans all operators in `redhat-operator-index:v<minor>`, but only
collects the **head bundle's `containerImage`** (1 image per operator) = operator control
plane source only. The actual substance of layered products like OPP/RHOAI/CNV is in the
**operand images** (separate images) listed in CSV `spec.relatedImages`, which are
currently unexpanded. Phase D recovers these using Phase A's "image→labels→GitHub archive"
approach.

## Scope (finalized)

| Item | Decision |
|------|------|
| Depth | All operands (full expansion of CSV `relatedImages`) |
| Targets | RHOAI / ACM+MCE / ACS / ODF+Quay / CNV |
| Rollout | PoC on latest 1 minor (OPP/CNV) → expand after verification |
| RHOAI version | Latest stable (channel head bundle, OCP minor independent) |
| PoC starting point | CNV @ latest minor (high recovery rate, fast verification). Starting minor to be decided between 4.20/4.21 |

### Target products and packages

| Product | Catalog package | Operand source recovery outlook |
|------|------|------|
| RHOAI | `rhods-operator` | Partial (internal git mixed) |
| ACM + MCE | `advanced-cluster-management` + `multicluster-engine` | Medium (most operands, largest size) |
| ACS | `rhacs-operator` | Medium–high (stackrox is OSS) |
| ODF + Quay | `odf-operator` (ceph/rook/noobaa) / `quay-operator` (clair, etc.) | Medium |
| CNV | `kubevirt-hyperconverged` | High (kubevirt/CDI are true OSS) |

## Architecture (reuses Phase A/C assets)

Approach, label priority, (repo,sha) dedup, extracted layout, and mksquashfs xz are
identical to existing phases. The only difference from Phase C is generalizing from
"1 image" to "all relatedImages (+ operator itself)".

```bash
phase-d-discover.sh     -p <pkg> -v <minor>                 # Pull head_bundle_image → extract CSV
                                                            #  → convert .spec.relatedImages[] to images.tsv
phase-d-fetch-source.sh -p <pkg> -v <minor> [--jobs N] [--limit N]
                                                            # oc image info for each image's labels
                                                            #  → git.tsv → GitHub tarball
                                                            #  (reuses phase-c-fetch-source label logic)
phase-d-package.sh      -p <pkg> -v <minor>                 # Extract stage → mksquashfs -comp xz
```

### Directory structure (follows Phase A/C conventions)

```
phase-d/<pkg>/<minor>/
├── 00-discover/   release/bundle CSV, images.tsv, labels.tsv, git.tsv
├── 20-git/        <name>-<short_sha>/...   (extracted GitHub archives)
└── 50-out/stage/  git/<name>-<sha>/  + meta/
```

### Artifacts (per-product; ACM is large so kept separate)

- `casket-<date>-cnv-ocp<minor>.sqfs.xz`
- `casket-<date>-acm-ocp<minor>.sqfs.xz` (+MCE bundled)
- `casket-<date>-acs-ocp<minor>.sqfs.xz`
- `casket-<date>-odf-ocp<minor>.sqfs.xz` / `casket-<date>-quay-ocp<minor>.sqfs.xz`
- `casket-<date>-rhoai-<2.x>.sqfs.xz` (version-independent, so filename uses RHOAI version)

## Milestones (PoC)

| # | Task | Verification point |
|---|------|------|
| 1 | Implement `phase-d-discover.sh` → extract relatedImages from CNV | Image count, digest resolution |
| 2 | `phase-d-fetch-source.sh` (reuse phase-c-fetch-source) → smoke test with `--limit` | Label→github resolution rate |
| 3 | Full fetch → aggregate recovery rate from git.tsv | NO_SOURCE count |
| 4 | `phase-d-package.sh` → generate .sqfs.xz, verify with `file`/mount | Size, extracted layout |
| 5 | After CNV is finalized, expand to ACS→ODF/Quay→ACM→RHOAI | Understand structural limitations per product |

## Notes and structural limitations

- **Recovery rate**: kubevirt/CDI (CNV), stackrox (ACS), ceph/rook/noobaa/clair
  (ODF/Quay) are true OSS with high expected recovery. RHOAI and ACM have mixed
  internal git (gitlab.cee etc.) and will be partial recovery like Phase C
  (`access.redhat.com` / internal hosts cannot be rescued).
- **Size**: ACM has the most operands. relatedImages can contain tens to hundreds,
  so final decision on ACM full collection will be made after PoC numbers.
- **Dedup**: Identical (repo,sha) across products/operands is unified (same
  `.work.tsv` approach as Phase A/C).
- **Mount operations**: Same pattern as existing `mount-new-minor.sh` / swap tools
  for fstab addition (`/srv/sources-<product>-ocp<minor>`). systemd unit names
  generated with `systemd-escape -p`.

## Next steps

1. Place CNV in production directory `/mnt/hdd/casket-ocp/` + fstab mount (same pattern
   as `mount-new-minor.sh`).
2. Expand to other products: ACS (`rhacs-operator`) → ODF/Quay → ACM+MCE →
   RHOAI (`rhods-operator`, stable). Check label trends per product and add
   `COMPONENT_MAP` entries as needed.
3. RHOAI/ACM are expected to have high internal git ratios. Extend
   `phase-d-resolve-labels.py` based on recovery rates.
4. CNV's `hostpath-*`/`ipam` can be additionally rescued via `COMPONENT_MAP` if
   upstream repos are identified.

## Decisions made

- PoC starting minor = **4.20** (executed 2026-06-03).
- Filename infix = product abbreviation (`-i cnv`, etc.).

## 2026-08-01: Batch fix for collection gaps and full minor rebuild

Started from a report that "OpenGrok is not indexing
`layered-4.18/service-registry-operator`". It was not an indexing bug but **a collection
gap**. Details are consolidated in CLAUDE.md's "B-operand source coverage" section.
Only numbers are kept here.

Before → after (products with actual source trees):

| minor | before | after |
|---|---|---|
| 4.18 | 49 / 146 | **121** / 146 |
| 4.20 | 52 / 149 | **120** / 149 |
| 4.22 | 49 / 140 | **111** / 140 |

For 4.18's 1355 operand images, of 769 resolved, **767 actually have source trees**
(exact 457 / approximate 310 / fetch failed 2). Before the fix, most "resolved" entries
had downloads failing with 404.

### Lessons learned from this work

- **`source_resolved` is not coverage**. Having a repo URL determined doesn't mean the
  source is obtainable — labels with commits that are internal konflux SHAs (not on
  public github) are very common. MANIFEST now splits into `source_exact` /
  `source_approx` / `source_fetch_failed`, and `meta/git-fetched.tsv` records actually
  fetched URLs with `exact` flag. Check this file before trusting a tree for CVE purposes.
- **Don't assume unfetchable `openshift/*` can be substituted from Phase A**.
  Of 82 unfetchable repos, only **17** are in the payload. metallb / sriov-\* / velero /
  ViaQ/\* / migtools/\* are layered-only.
- **Candidate chain rescues are mostly branch heads**. Measuring 4.18's 156 unfetchable
  repos: all 156 got something, but **155 were main/master/release-X**. That's why they
  are explicitly recorded as approximate.
- **Only add to `COMPONENT_MAP` when upstream tags match the version**. Products where
  only main/master is available (amq-broker, strimzi, noobaa, 3scale, Quay, Kuadrant
  subcomponents) can identify the repo but have different versioning schemes, so they
  are **intentionally unmapped**. Reasoning is documented in code comments.
- `konflux-ci/mintmaker` is a **build bot** that KMM 4 images carry in their source
  label. Rejected via `INFRA_REPOS` and named as `z:infra-label` (not silently dropped).

### Measured build times

All 9 minors took about 14 hours, **averaging 1.5 hours / minor**. The early estimate
of "8 hours/minor" from initial samples was too conservative. Most time is spent on
SKIPping already-fetched tarballs.
