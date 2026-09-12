# Design decisions and known caveats

The approach chosen for each phase and the reasoning behind it.

## Phase A

- **Why `*-source` containers were rejected**: The source containers tied to the OCP release payload have unstable naming conventions. Probing pullspecs like `quay.io/openshift-release-dev/ocp-v4.0-art-dev:<...>-source` on real clusters returned 404 in all cases. Instead, `oc adm release info --commits` cleanly returns the GitHub repo URL + exact commit SHA for each component, so this was adopted as the source of truth.
- **Direct GitHub archive download**: Tarballs are fetched from `https://github.com/<owner>/<repo>/archive/<sha>.tar.gz` — no authentication required, one request per component. ~95 seconds for 164 items.
- **Dedup by (repo, commit)**: Of 191 images, ~27 share the same repo+commit (e.g. multiple CSI driver operators using `csi-operator`). Tarballs are deduped for storage, with MANIFEST.json maintaining the many-to-one image→tarball mapping.
- **What `.sqfs.xz` actually is**: Examining existing caskets (`casket-20251118-rhel10.sqfs.xz` etc.) with `file` / `xz -t` confirmed they are **internally xz-compressed squashfs with no outer xz wrap**. This project uses the same format.

## A-rpm

- **`--rpmdb-image=rhel-coreos` is required**: 4.14/4.15 have both `machine-os-content` (old el8-based ostree) and `rhel-coreos` (new el9-based OCI) coexisting. The default behavior of `oc adm release info --rpmdb` picks the former, and this project's subscription (RHEL 9 only) cannot fetch SRPMs for it. Explicitly specifying `rhel-coreos` processes all 7 versions as **el9-unified**.
- **rpmdb extraction runs inside a RHEL 9 VM**: The Fedora host's newer rpm fails at the `rpm -qa` stage for 4.14-4.18 rpmdb conversion (format mismatch with host rpm). A VM with RHEL 9 native rpm handles all versions.
- **SRPMs fetched via `dnf download --source`**: Given binary RPM NEVRs, dnf resolves the sourcerpm through repodata and saves the `.src.rpm`. Same-name SRPMs in the output directory are naturally deduped.
- **EUS / E4S requires explicit `--releasever=<minor>`**: SRPMs for el9_2 / el9_4 / el9_6 errata are only available through EUS-specific channels, and a VM running 9.8 fetches `eus/rhel9/9.8/...` which 404s. Re-fetching with `--releasever=9.2` etc. per dist-tag minor improved coverage from the initial 32% to a final 99.86%.

## Phase B

- **Target is the `vN.M` tag of redhat-operator-index**. Phase B operates per **minor** (unlike Phase A's per-patch granularity), because the catalog is tagged that way.
- **File-Based Catalog (FBC) lives in `/configs`**: No need to run an opm server — `oc image extract --path /configs/:dest/` retrieves the NDJSON directly.
- **Head bundle computation considers `replaces` + `skips`**: Finds entries not referenced by any other entry's `replaces` / `skips` via set operations. When multiple remain, takes the last in the array.
- **Container image labels are inconsistent**: `io.openshift.build.source-location` + `vcs-ref` should be present but often aren't. Phase B v2 (CSV-based + branch fallback) improved resolution from 6 to 34 (86 for 4.20). Remaining misses are operators whose CSV `repository` doesn't point to github — built via brew/cachito, making them unreproducible as public tarballs.

## Troubleshooting

- **GitHub rate limits**: No issues observed when running 164 targets at parallelism 6 without authentication (archive downloads are under a separate rate limit from the API). If 429 errors occur, reduce `--jobs` or set `GITHUB_TOKEN`.
- **Phase A/RPM VM**: `rhel9-srpm` (libvirt) is persisted in a registered and configured state. `sudo virsh start rhel9-srpm` → `sudo virsh net-dhcp-leases default` to get the IP.
- **2 uncollected SRPMs**: `redhat-release-9.2-0.15.el9` / `redhat-release-eula-9.2-0.15.el9` have disappeared even from EUS channels (intermediate versions just before 9.2 EUS EOL). No practical impact.
