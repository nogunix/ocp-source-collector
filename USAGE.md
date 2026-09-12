# ocp-source-collector user guide

Reference for browsing OpenShift / RHCOS source code from `/srv/sources-*`.

## Mount points

Permanently mounted via `/etc/fstab`. Auto-mounted after reboot, `ro` (read-only).

| Mount point | Contents | Origin |
|---|---|---|
| `/srv/sources-ocp4.14.58` through `/srv/sources-ocp4.20.22` | OCP component github sources (one mount per minor, 8 total) | Phase A |
| `/srv/sources-ocp-srpms` | rhel-coreos SRPM extracted sources (union of all 7 OCP versions, 843 SRPMs) | A-rpm |
| `/srv/sources-ocp4.14-operators` through `/srv/sources-ocp4.20-operators` | OperatorHub redhat-operators bundles + source (one mount per minor, 8 total) | Phase B |
| `/srv/sources-rhel9` / `/srv/sources-rhel10` | Existing RHEL caskets (for reference) | Separate system |

List all mounts:

```bash
mount | grep /srv/sources-
```

---

## Layout cheatsheet (all 3 phases: fully extracted, browseable with `cd`)

### Phase A — OCP component sources

`/srv/sources-ocp<X.Y.Z>/`

```
git/<name>-<short_sha>/   ← github source for each component (pinned to commit)
  cmd/ pkg/ ...           ← github archive top-level dir stripped, source immediately accessible
meta/
  MANIFEST.json           image -> tarball mapping (with sha256)
  commits.tsv             name TAB repo_url TAB commit_sha
  images.tsv              name TAB pullspec TAB digest
  release.json            oc adm release info --output=json
```

### A-rpm — SRPM extracted sources

`/srv/sources-ocp-srpms/`

```
srpms/<NEVR>/             NEVR = name-version-release (e.g. bash-5.1.8-9.el9)
  <name>.spec             RPM spec
  <tarball-top-dir>/      upstream source extracted (from Source* tarballs, multiple subdirs if multiple)
  patches/                .patch files + auxiliary files
meta/
  MANIFEST.json           SRPM index + binary RPM list per OCP version
  rpmdb/<OCPver>.tsv      name TAB epoch TAB ver TAB rel TAB arch
  README.txt
```

The `.src.rpm` files themselves are not included (use rhel-coreos rpmdb to re-obtain if rpmbuild is needed).

### Phase B — Operator bundle + source

`/srv/sources-ocp<X.Y>-operators/`

```
catalog/<operator>/catalog.json    File-Based Catalog (FBC) verbatim
bundles/<operator>/<head>/
  manifests/                       CSV + CRDs (OLM payload)
  metadata/                        annotations.yaml
git/<name>-<short_sha>/             github source (extracted)
meta/
  MANIFEST.json
  operators.tsv                    name TAB channel TAB head_bundle TAB head_image
  containers.tsv                   name TAB head_bundle TAB containerImage TAB csv_version
  labels.tsv                       containerImage TAB source_url TAB vcs_ref
  git.tsv                          name TAB source_url TAB vcs_ref TAB tarball|NO_SOURCE
```

---

## Common operations

### Open the source for a package

```bash
# OCP component (e.g. etcd in 4.20.22)
cd /srv/sources-ocp4.20.22/git/etcd-*/
ls

# SRPM (e.g. bash)
cd /srv/sources-ocp-srpms/srpms/bash-5.1.8-9.el9/bash-5.1/
ls

# Operator (e.g. OpenShift GitOps in 4.20)
cd /srv/sources-ocp4.20-operators/git/gitops-operator-*/
```

### Fuzzy package name lookup

```bash
# Phase A: list all components
ls /srv/sources-ocp4.20.22/git/ | sort

# A-rpm: list available SRPMs
ls /srv/sources-ocp-srpms/srpms/ | sort

# Partial match search (works for any phase)
ls /srv/sources-ocp-srpms/srpms/ | grep -i kernel
```

### Grep across all SRPMs (e.g. find a function definition)

```bash
grep -r "function_name" /srv/sources-ocp-srpms/srpms/ 2>/dev/null | head
```

squashfs is cached in memory, so repeated greps are fast. The first run involves disk reads though, so narrowing with `--include` helps:

```bash
grep -r --include='*.c' "func_name" /srv/sources-ocp-srpms/srpms/
```

### Find which SRPM a binary RPM comes from

```bash
# Reverse lookup from rpmdb (get NEVR)
grep -i '^cri-o' /srv/sources-ocp-srpms/meta/rpmdb/4.20.22.tsv

# → the resulting name-ver-rel corresponds to srpms/<NEVR>/
ls /srv/sources-ocp-srpms/srpms/cri-o-*
```

### Check a component's commit hash (Phase A)

```bash
grep ^etcd /srv/sources-ocp4.20.22/meta/commits.tsv
# → etcd  https://github.com/openshift/etcd  <full_sha>
```

The directory name `etcd-<short_sha>` uses the first 12 characters of the sha from `commits.tsv`.

### View an operator's CSV (Phase B)

```bash
cd /srv/sources-ocp4.20-operators/bundles/openshift-gitops-operator/
ls */manifests/*.clusterserviceversion.yaml
```

The CSV is a single YAML/JSON file. Extract attributes with `yq`/`jq`:

```bash
yq '.spec.install.spec.deployments[].spec.template.spec.containers[].image' \
    bundles/openshift-gitops-operator/*/manifests/*.clusterserviceversion.yaml
```

### Programmatic lookup via MANIFEST.json

```bash
# Phase A: tarball filename for an image
jq '.images[] | select(.name=="etcd") | .tarball' \
   /srv/sources-ocp4.20.22/meta/MANIFEST.json

# A-rpm: sha256 for an SRPM
jq '.srpms[] | select(.nevr=="bash-5.1.8-9.el9") | .sha256' \
   /srv/sources-ocp-srpms/meta/MANIFEST.json

# Phase B: source_url for an operator
jq '.git[] | select(.operator=="openshift-gitops-operator")' \
   /srv/sources-ocp4.20-operators/meta/MANIFEST.json
```

---

## Limitations and notes

| Item | Details |
|---|---|
| `ro` mounts | All caskets are read-only. To modify, `cp -r` to another path |
| A-rpm coverage | 2 binary RPMs (`redhat-release-9.2-*`) are uncollected due to expired EUS channels |
| Phase B source resolution | Operators whose CSV repository points to `access.redhat.com/containers/...` cannot have github tarballs fetched (50-60 per minor). Listed as `NO_SOURCE` in `meta/git.tsv` |
| Phase A vs B granularity | Phase A is per OCP **patch** version (e.g. 4.20.22), Phase B is per **minor** (e.g. 4.20). Distinguished by mount name |
| File ownership | `mksquashfs -all-root` sets all files to `root:root` with `a+r` permissions |
| No direct `rpm -i` | A-rpm contains extracted source only, not the `.src.rpm` files. Re-obtain from rhel-coreos rpmdb if `rpmbuild` is needed |

---

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `/srv/sources-*` is empty / unmounted | `sudo systemctl restart srv-sources\\x2d<...>\\.mount` (generate path with systemd-escape) |
| `ls` is slow | First directory read is slow. Subsequent reads are fast due to caching |
| `grep -r` uses too much memory | Narrow with `--include`, or limit scope to `git/<one>` |
| Suspected file corruption | Verify squashfs integrity with `unsquashfs -s /mnt/hdd/casket-ocp/casket-*.sqfs.xz` |

---

## Exporting data to another machine

```bash
# Copy a single SRPM directory
cp -r /srv/sources-ocp-srpms/srpms/bash-5.1.8-9.el9 /tmp/

# Transfer the casket file to another host (3.5G for A-rpm)
scp /mnt/hdd/casket-ocp/casket-20260528-ocp-srpms.sqfs.xz user@otherhost:/path/
# On the destination:
#   sudo mount -o loop /path/casket-20260528-ocp-srpms.sqfs.xz /mnt/sources
```

`.sqfs.xz` is internally xz-compressed squashfs (no outer xz wrap). Directly mountable with `mount -t squashfs`.

---

## Related documentation

- `README.md` — System overview, build instructions, design decisions
- `CLAUDE.md` — Maintainer project conventions
- `meta/README.txt` inside each casket — Per-phase quick reference
