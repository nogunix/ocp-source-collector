# Phase B (formerly Phase C) — Uncollected operator recovery plan

> "Phase C" in this document refers to what was renamed to "Phase B" on 2026-07-11. Original names are preserved for historical accuracy.

> Created: 2026-06-09 / Updated: 2026-06-09 (reflects 4.18 actual pipeline investigation and fixes)
> Target: OLM operators in OCP `redhat-operator-index` not yet collected by casket
> Investigation base: OCP 4.18 index (discover→fetch-bundles already executed, cache available)

## Background — Phase C processes the entire index

`phase-c-discover.sh` processes the entire index. 4.18 actual measurements: catalog has
153 operators, 122 with default-channel heads. Only those whose source resolves to a
fetchable commit/tag on public GitHub are collected (production 4.18 casket: ~34
directories).

## Two root causes (both fixed)

### Cause 1 — ref cannot be resolved (resolve-v2.sh)

Repository URL is obtainable from CSV, but internal build `revision` SHAs don't exist
on public mirrors, and v2's existing fallback (`v<version>` / `release-<ocp-minor>` /
main / master) returns 404 due to **product version vs public repo tag naming
mismatches**.

**Fix**: Added per-family ref strategies (case table, same pattern as Phase D
COMPONENT_MAP) to `phase-c-resolve-v2.sh`'s `dl_one`. Derives `ver_minor` (product
version MAJOR.MINOR) for use.

| Repository | Transform | Actual (4.18) |
|---|---|---|
| redhat-developer/gitops-operator | `tags/v<MAJOR.MINOR>.0` (patch truncated) | v1.20.0 → 200 |
| maistra/istio-operator | `tags/maistra-<ver>-dev`, `tags/maistra-<ver>` | maistra-2.6.16-dev → 200 |
| openshift-knative/serverless-operator | `heads/release-<MAJOR.MINOR>` (from product version) | release-1.37 → 200 |
| ComplianceAsCode/compliance-operator | `tags/v<ver>`, `heads/<MAJOR.MINOR>` | (approximate, RH is ahead of public tags) |

### Cause 2 — rows dropped when containerImage annotation is missing (fetch-bundles.sh) ★ discovered in this investigation

`phase-c-fetch-bundles.sh:149`'s `[[ -z "$container" ]] && continue` was excluding
operators without a `containerImage` annotation from `containers.tsv`. 4.18 actual:
only **92 of 122 rows**, **30 operators dropped**. Dropped operators never reach
resolve-v2, making them unrecoverable regardless of ref strategy. The 30 dropped
include serverless / pipelines / compliance / file-integrity / sandboxed-containers /
windows-machine-config, etc.

**Fix**: Removed the `continue`. Rows with empty containerImage are kept. resolve-v2
re-extracts csv_repo from `metadata.annotations.repository` (or `spec.links[].url`)
to resolve.

### Cause 3 — read column shift on empty ref (resolve-v2.sh download section) ★ discovered in this investigation

Operators with only csv_repo and no commit label have an empty ref. The DL_TSV row
becomes `src<TAB><TAB>ver<TAB>fname`, and `IFS=$'\t' read -r src ref ver fname`
**collapses consecutive tabs into one** (tab is whitespace), causing column shift so
`fname` becomes empty → downloads for all 29 affected products were silently
invalidated.

**Fix**: Replace empty ref with `_NONE_` sentinel during DL_TSV generation; restore to
empty at the start of `dl_one`. This correctly handles the empty-ref case for
serverless/pipelines/compliance, etc.

## 4.18 measured summary (post-fix, all 3 causes resolved)

| Metric | Value |
|---|---|
| Production 4.18 (pre-fix) | 34 source dirs |
| **Post-fix unique source dirs** | **110** |
| Collected operators (before dedup) | 114 |
| Head operators | 122 |
| NO_SOURCE | 6 |
| DL failure | 2 (openstack=org-root, openshift-builds=www.redhat.com) |

**~3.2x improvement (34→110).** Priority 3 products resolved as intended:
serverless→release-1.37, gitops→v1.20.0, servicemesh→maistra-2.6.16-dev.
Source contents verified (tektoncd operator-main, serverless release-1.37, etc. are
real source trees).

Quality: Most use sha/tag/release-branch for exact version match. Some (~12–14:
compliance/pipelines/kueue/lws/jobset/node-maintenance/security-profiles/watcher/logic/
orchestrator/exploit-iq/amq-broker) are main/master approximations (latest source, not
exact match to shipped version). This is the same policy tradeoff as existing v2.

## 4.18 earlier measurements (during cause investigation)

- Catalog 153 / head 122 / old containers.tsv 92 (→ post-fix 122)
- Of the 30 dropped: **18 have GitHub csv_repo** (rescue candidates), 0 non-GitHub,
  12 have no repository annotation
- Of the 12 without repository annotation, most have GitHub in `spec.links` (see table
  below)

## Full rescue picture (4.18)

### A. GitHub csv_repo (18 products) — fix#2 makes them reach resolve-v2

serverless, openshift-pipelines, compliance, file-integrity, sandboxed-containers,
windows-machine-config, jobset, lws, node-maintenance, fence-agents-remediation,
machine-deletion-remediation, logic-operator (×2), orchestrator, rhtpa, exploit-iq,
amq-broker (rhel8/rhel9).
openshift/* and medik8s/* have release branches/tags/sha available for easy resolution.

### B. No repository annotation — 12 products via spec.links (actual measurements)

| Group | Count | Products | Resolution |
|---|---|---|---|
| B1: No additional code needed, version-exact | 3 | dpu, nbde-tang-server, numaresources | `release-4.18` branch exists (200) |
| B2: No additional code needed, latest approximation | 5 | cincinnati, devworkspace, kueue, security-profiles, watcher | Only main/master returns 200 (latest approximation) |
| B3: Org-root link only | 1 | openstack | Stops at `openstack-k8s-operators/`. Needs Phase D-style manual repo mapping |
| B4: No github link | 3 | ansible-automation-platform, ansible-cloud-addons, pf-status-relay | Requires extracting `io.openshift.build.source-location` label from control image. ansible likely has no public source |

B1+B2 (8 products) require **no additional code** (fix#2 + existing spec.links
fallback handles automatic collection).

## Difficult 2 products (exact match impossible, approximation only)

| Product | Reason |
|---|---|
| openshift-pipelines (tektoncd/operator) | Product version v1.22 ↔ upstream v0.79 are completely incompatible. No version reverse-mapping exists. Needs midstream repo investigation |
| compliance (ComplianceAsCode) | RH shipped v1.9.0 has no public tag/branch (public latest is v1.8.2). Master approximation only |

## Resolution outlook (4.18)

| Category | Count | Notes |
|---|---|---|
| Existing collected | ~34 | Maintained |
| Group A (GitHub csv_repo) | ~16/18 | Most resolved except 2 difficult cases |
| Group B1+B2 | 8 | Automatic collection (3 version-exact / 5 approximation) |
| **Estimated total** | **~46–50** | +35–45% over current |
| Remaining issues | 4 | openstack (B3) + ansible×2/pf-status-relay (B4) |

## Implementation steps

1. **Done** — Per-family ref strategies in `phase-c-resolve-v2.sh` (cause 1)
2. **Done** — Removed empty containerImage row drop in `phase-c-fetch-bundles.sh` (cause 2)
3. **Done** — Empty ref sentinel fix in `phase-c-resolve-v2.sh` (cause 3)
4. **Done** — 4.18 full re-run → 110 unique dirs (34→110) confirmed
5. Package 4.18 → verification mount (`phase-c-package.sh -v 4.18`)
6. Re-run + repackage all 8 minors (4.14–4.21): discover→fetch-bundles→fetch-source→resolve-v2→package per minor, `swap-operators-extracted.sh` (same-name swap, fstab unchanged)

### Deferred (next issues)

- openstack: Manual mapping of constituent repos (Phase D approach)
- ansible×2 / pf-status-relay: Control image build label extraction logic (low cost-effectiveness)
- pipelines: Product version↔upstream version mapping table or RH midstream repo investigation
- compliance: Wait for public tag or accept master approximation

## Verification commands (for reproduction)

```bash
# Extract a single operator's config (no need for full index)
mkdir -p /tmp/cfg/<op>
oc image extract --registry-config=~/.docker/config.json --filter-by-os=linux/amd64 \
  --path "/configs/<op>/:/tmp/cfg/<op>/" registry.redhat.io/redhat/redhat-operator-index:v4.18

# Check head bundle CSV's repository / spec.links / containerImage
oc image extract ... --path "/manifests/:/tmp/b/<op>/" <bundle-image>
python3 -c "import yaml; d=yaml.safe_load(open('<csv>'));
ann=d['metadata']['annotations']; print('repo=',ann.get('repository'));
print('links=',[l['url'] for l in d.get('spec',{}).get('links',[])])"

# Verify ref candidate existence
curl -sIL -o /dev/null -w '%{http_code}\n' \
  https://github.com/<owner>/<repo>/archive/refs/heads/release-4.18.tar.gz
```
```bash
# zsh note: unquoted variables don't undergo word splitting. Use printf '%s\n' ... | while read for loops.
```
