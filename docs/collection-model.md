# Source code collection model definition

> Deliverable from the 2026-07-16 consultation: "Define the source code collection work in a format that AI can manage." **This single document is the map of the entire collection system** — AI (Claude Code etc.) can use it as the entry point to reproduce, audit, and extend all collection operations. Individual procedure details are authoritative in their respective linked documents.

## 1. Collection target layer model

"All OpenShift source" is defined as 2 origins × 2 depths each + 1 cross-cutting layer = 5 layers.

```
Origin 1: Release payload (quay.io/openshift-release-dev)
  a       Git source of payload containers         (oc adm release info --commits is the source of truth)
  a-rpm   rhel-coreos rpmdb → node OS SRPMs        (one level deeper)
Origin 2: Operator catalog (registry.redhat.io/redhat/redhat-operator-index)
  b       Operator source + bundles for all packages (certified/community catalogs use the same mechanism)
  b-operand  CSV relatedImages for all packages → operand source (one level deeper)
Cross-cutting: RPMs inside container images
  image-rpms  rpm-manifest of all collected images → SRPMs (Pyxis API, no pull required)
```

- An operator manages CRs; the **operand is the actual product** (CNV: 1 operator vs 61 operands).
- "Above" a container is git, "below" a container/node (OS) is RPM — this material difference is what creates the a↔a-rpm / b,b-operand↔image-rpms pairings.

## 2. Machine-readable definition files (what to collect)

| File | Meaning | Format |
|------|---------|--------|
| `config/minors.txt` | OCP minors to track | 1 minor per line |
| `config/phase-b-operand-products.tsv` | b-operand targets = **all catalog packages** (package→infix; 9 curated products have short names) | TSV |
| `config/phase-a-rpm-minors.txt` | A-rpm target minors | 1 minor per line |
| `config/auto-update-phases.txt` | Phases for automatic staging | 1 phase per line |
| `config/static-mounts.tsv` | Static caskets outside the registry (e.g. 2 RHEL caskets) | TSV |
| `config/opengrok-minors.txt` | Minors whitelisted for OpenGrok indexing | 1 minor per line |

## 3. Upstream source location records (where it came from)

Generated per build and **bundled inside the casket** (= self-describing archive):

| Record | Location | Contents |
|--------|----------|----------|
| `git/INDEX.tsv` | Each casket | dir↔repo↔ref↔version↔components (+ by-component/ by-repo/ symlinks) |
| `meta/git.tsv` `meta/labels.tsv` | b / b-operand | component↔image↔source_url↔ref↔**resolution method** (a:upstream-vcs / b:component-map / c:source-label / f:caa-versions-pin …) |
| `meta/IMAGE_MAP.tsv` | b-operand | image pullspec → component → git dir |
| `meta/bin-to-src.tsv` `by-ocp/` | a-rpm | binary NEVRA → source NEVR → OCP version |
| `meta/image-rpms.tsv` | a-rpm (extended) | image digest → SRPM NEVR (in-container RPM layer) |
| `COMPONENT_MAP` | `scripts/phase-b-operand-resolve-labels.py` | **Explicit documentation** of exceptions unresolvable from labels (image name → repo → mode) |

## 4. Freshness and updates (when to re-collect)

- Freshness signal: `scripts/lib-fingerprint.sh` (a = stable channel patch string / b, b-operand = index digest / a-rpm = sha256 of patch strings bundled). check and build share the same implementation.
- **Automatic staging every 3 weeks** (changed from weekly on 2026-09-12): `casket-auto-update.timer` (wakes up Friday 23:30 JST, runs once every 3 weeks → completes staged build in 2.5-3 days → manual swap). swap/cleanup always require human action (`docs/operations.md`).
- Catalog digests rotate daily (treadmill). "STALE immediately after swap" is normal behavior.

## 5. Completeness invariants (machine-verifiable collection coverage)

**Lesson learned**: Silent `continue` statements caused two large-scale omissions (2026-06-09: containerImage 30/122, 2026-07-17: FBC format drift 50/153). Never claim completeness from spot checks.

| Verification | Implementation |
|---|---|
| All catalog packages vs operators.tsv denominator check | `phase-b-discover.sh` outputs named items to `uncovered.txt`, prints "coverage: all N captured" when 0 |
| FBC format normalization | Supports 4 formats (catalog.json / split JSON / arbitrary names / catalog.yaml+datetime) → all normalized to NDJSON |
| b-operand expected products vs actual collection | `casket-mcp coverage_report` (missing_b_operand_products + known_gaps) |
| Named uncollectables | `known_gaps` (pipelines/compliance version mapping etc.) + `image-rpms-unfetchable.txt` |

## 6. Fetching mechanisms (how to collect)

| Target | Method |
|--------|--------|
| Git source | GitHub archive (commit preferred, tag fallback. `dl_one`) |
| SRPM (node OS) | RHEL9 VM `rhel9-srpm` + dnf with per-`--releasever` source repos |
| SRPM (in-container) | **CDN direct**: `repoquery --arch=src --location` + `curl --cert <entitlement>` (el8/el9/el10/layered all streams. `dnf download` rejects src arch, so not usable) |
| Image RPM list | **Pyxis API** (no pull required. Payload uses image_id / registry.redhat.io uses manifest_list_digest) |
| Repackaging | Overlayfs incremental (`repackage-srpms-refresh.sh` — avoids re-extracting 70G) |

## 7. AI operational conventions

1. When modifying the collection system, **always verify denominator check output** (uncovered.txt / coverage_report).
2. Never silently `continue` on unexpected formats — count them and WARN (silent-skip prohibited).
3. Production swap and cleanup always require human `--apply`. AI/automation scope ends at staging.
4. Same-UTC-day rebuilds overwrite artifacts with the same name → after swap, `registry.py remove` duplicate entries for the same path (prevents cleanup from deleting a live entry).
5. The registry (`state/registry.json`) must only be modified through `scripts/registry.py`. Hand-editing prohibited.

## 8. TODO (remaining formalization items)

- [x] Weekly link health check via GitHub Actions (implemented 2026-07-18):
      `scripts/export-upstream-sources.py` flattens all casket INDEX.tsv files into
      `state/upstream-sources.tsv` (repo↔ref↔kind↔first-seen-casket), and
      `.github/workflows/upstream-link-check.yml` (Saturday 09:00 JST) runs
      `scripts/check-upstream-links.py` checking API (repo existence/rename) + codeload
      HEAD (ref re-fetchability). Collected content is safe inside caskets, so
      detections indicate "re-fetchability warnings".
      **Moved to casket-host on 2026-09-23**: `state/` is host-local and never
      committed to the public repo (since 2026-09-12), so the Actions job had no
      manifest to read and failed every week. It now runs as
      `systemd/upstream-link-check.timer` (Wed 09:00) via
      `scripts/upstream-link-check.sh`, which regenerates the manifest from
      `/srv` on every run, so it can no longer go stale. The workflow stays as a
      fallback that skips with a notice when the manifest is absent. See
      `docs/operations.md`.
      **Pipeline-faithful candidates (2026-09-23)**: B / B-operand rows record
      the image's vcs-ref, which is often a Konflux-internal commit absent from
      public GitHub, while the pipelines fall back through tags and branches.
      The export now writes each such row's `candidate_source_urls()` chain
      (called from `lib-resolve.sh`, not re-implemented) into a 6th manifest
      column, cut after the candidate B-operand actually won
      (`meta/git-fetched.tsv`). Not-re-fetchable rows dropped from 1,388 to
      ~80 on the first run.
- [ ] z:none remaining items systematic audit: Collect z:none × vcs-ref present
      from all layered labels.tsv, use GitHub commit-hash search API
      (`/search/commits?q=hash:<sha>`) to auto-identify owning repos →
      semi-automatic COMPONENT_MAP generation (automating the pattern validated
      manually with OSC/netobserv. Rate limit 30 req/min, suitable for overnight batch)
- [ ] Tree-sitter backend evaluation (Yamato-san's suggestion: improve analysis
      quality while maintaining compatibility with ctags output format.
      An option for enhancing casket-mcp's search_symbol)
