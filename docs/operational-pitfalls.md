# Operational pitfalls

Hard-won lessons from building and operating the casket fleet. Each section
documents a specific trap, its symptoms, and the fix or guard now in place.
Moved here from CLAUDE.md to keep the top-level file concise.

## Dependency collection

### Cross-filesystem hardlink trap ($CASKET_DEP_STORE)

Sizes are real: one operators casket = ~24G of extracted deps, but squashfs xz
hits ~10x on this content (717M → 69M measured), so ~+2-3G per casket.
`$CASKET_DEP_STORE` + its sibling `-cache` live on the HDD for that reason
(production: `/mnt/hdd/casket-dep-store`), and the overlay upper dir MUST be
on the same filesystem or the hardlinks degrade to full copies into tmpfs.
**That HDD path is the production setting, NOT the code default** — this
file used to claim it was, and that cost 160G. `collect-deps.py` falls back
to `$CASKET_WORK/dep-store`, i.e. into the repo on the root fs, whenever the
var is unset. A systemd unit or a `systemd-run` job starts from a minimal
environment and so misses it every time: the 2026-07-31 weekly auto-update
did exactly that, took root from 495G to 264G free and re-downloaded every
archive `/mnt/hdd/casket-dep-store` already held. The unit now sets it
(`systemd/casket-auto-update.service`, same as it already did for PATH) and
the fallback warns loudly. Moving a stray store off the root fs does NOT
free the space, because collect-deps hardlinks store → stage: the stage's
link keeps the data alive until the work dirs themselves are deleted.

`$CASKET_SUBMODULE_STORE` is the same trap: staging is store→stage hardlinks,
so a store on a different fs from the stage degrades to full copies.

### Manifest matching quirks

Real repos do not use textbook filenames: openshift/ironic pins in
`python-requirements.okd`, agent-installer-ui locks with a **berry** yarn.lock
(`resolution: "pkg@npm:1.2.3"`, no URL at all — the tarball URL has to be
rebuilt from the registry layout). Manifest matching is fnmatch, not fixed names.

proxy.golang.org 404s without the module-path case encoding (`Azure` →
`!azure`), for both the module path and the version.

go.sum lists every version in the module *graph*; Go's MVS compiles exactly one
per module path. Keeping them all pulled 1.9G of unused azure-sdk-for-go
(v46/v57/v67/v68) into a single operator casket, so `resolve_go_sum` keeps the
max per path. (Across trees the union barely shrinks — different components
legitimately select different versions.)

Unpinned requirements (`>=`, or bare names) have no single right answer offline.
They are NOT silently dropped: the manifest is named in DEPS-uncovered.txt.

### One malformed lockfile must never abort a casket

`scan_tree` yields a resolver exception instead of raising it and
`collect-deps.py` guards each tree, so a bad manifest costs only its own deps
and is named in DEPS-uncovered.txt. This was learned the expensive way: a
`"resolved": false` (legal in npm lockfile v1) in one community operator
crashed seven caskets, ~7h of rebuild.


## Submodule collection

### GitHub archive ships empty submodule dirs

A GitHub `/archive/<sha>.tar.gz` is `git archive` output, and `git archive`
writes a gitlink as an **empty directory**. Nothing in the tarball carries the
submodule's content *or its pinned commit*, so every tree that uses submodules
reached the caskets as build glue with the code missing. Reported as "ZTWIM's
operator implementation is inaccessible", and ZTWIM is the worst shape of it:
`openshift/zero-trust-workload-identity-manager-release` is **nothing but**
8 Containerfiles, a Makefile and 5 empty submodules, so the CRDs
(`api/v1alpha1/*_types.go`, `bundle/manifests/*.yaml`) and the controllers
(`pkg/controller/*/`) were absent from all 5 minors that claimed to carry it.
It is not a ZTWIM bug and not an OpenGrok bug: a fleet scan found **1052 trees
with `.gitmodules` and all 2543 of their submodule dirs empty**, across
`4.14-4.22` operators (59-73 each) and layered (`cluster-observability-operator`
44 each) — including `openshift/cert-manager-operator`,
`rhobs/observability-operator`, `openshift/monitoring-plugin`,
`openshift/thanos`, `korrel8r/korrel8r`. Only **153 distinct submodule repos**
and 9 non-GitHub refs, so it is cheap to close.

`scripts/collect-submodules.py` (+ pure resolvers in `scripts/submodulelib.py`,
tests in `tests/test_submodules.py`) runs on the STAGE tree just before
`collect-deps.py` — so one implementation serves Phase A, B, certified,
community and B-operand, and the filled-in trees then get their own deps
collected too. It fills each empty dir and writes `meta/SUBMODULES.tsv`
(component | path | repo | ref | exact | status) plus
`meta/SUBMODULES-uncovered.txt`. `CASKET_COLLECT_SUBMODULES=0` skips it; it
never fails the build.

### Pinned commit recovery via GitHub API

- The pinned commit is **only** in the parent's tree object, as a **mode
  160000** entry: `api.github.com/repos/<slug>/git/trees/<sha>?recursive=1`.
  The *contents* API is useless for this — it reports a submodule as type
  `"file"` with the submodule's own sha in a field that looks like a blob.
- That means one API call per parent tree, and **unauthenticated GitHub allows
  60/hour** while a single operators casket needs ~70. Without
  `GITHUB_TOKEN`/`GH_TOKEN` almost everything silently degrades to the
  `.gitmodules` **branch head**, which is whatever is there today and not what
  was built. Those rows are fetched anyway but recorded `exact=0` and logged
  APPROX — the same exact/approx split `phase-b-operand-fetch-source.sh`
  already uses. The script warns loudly when the token is missing, and
  `casket-auto-update.sh` falls back to `gh auth token` so the unattended run
  needs no token file (a user unit runs as the invoking user, so
  `~/.config/gh` is readable); an exported `GITHUB_TOKEN` still wins.
- URL forms in the fleet are 3: `https://github.com/o/r(.git)` (2507),
  scp-style `git@github.com:o/r.git` (27), and `https://gitlab.com/...` (9).
  The gitlab ones are named in SUBMODULES-uncovered.txt, never dropped.
  Relative URLs (`../peer.git`) resolve against the *superproject's remote*,
  so `../../other/peer.git` legitimately crosses to another owner.
- Never overwrite a submodule dir that already has content — some trees do
  ship a populated one, and the archive is the authority there.

### Filled submodule trees are invisible to the index

(2026-09-07) `git/INDEX.tsv`, `by-component/` and `by-repo/` name only the
top-level dir, which for these trees is the wrapper (`<name>-release`). A
JANUS source-trace run concluded "the Red Hat ZTWIM operator is not in the
casket" off `resolve_component`, having found only the wrapper's
`openshift/spiffe-spire` submodule — while the operator's `api/`,
`pkg/controller/` and `bundle/manifests/` sat one level down in the same
tree, in all of 4.18-4.22. `mcp/backends.py` now reads `meta/SUBMODULES.tsv`
alongside the indexes in `resolve_repo`/`resolve_component`/`list_components`
(tests: `tests/test_mcp_resolve.py`): rows whose dir is not on the mount are
dropped, one `(repo, ref)` reachable under several sibling wrappers collapses
to one hit, and `exact=1` outranks a branch-head row. The **permalink** is the
other half of the trap — INDEX.tsv's repo/ref for a submodule path points at
the wrapper, which does not contain the file; use the SUBMODULES.tsv row's own
repo/ref. Neither the caskets nor OpenGrok changed, only the resolution.

### Certified/community catalogs were NOT backfilled (decision 2026-09-07)

They are the 18 caskets built before submodule collection existed, and the
obvious follow-up — run `repackage-add-submodules.sh` over them — was started
and abandoned after the first artifact came back `filled 0/0`. Certified cannot
be filled at all: the parent commits are konflux-internal (`git/trees` 422) so
nothing pins, and their `.gitmodules` carry no `branch` for `plan_submodules`
to fall back to; what is missing is mostly Hugo doc themes (`asmacdo/docsy` x89
fleet-wide, `h-enk/doks` x45, `citrix/devdocs-theme`). Community does fill —
WMCO writes `branch = release-<minor>` — but its submodules are
`openshift/{kubernetes, ovn-kubernetes, cloud-provider-aws,
cloud-provider-azure, containernetworking-plugins}`, which the same minor's
**payload** casket already carries at the exact release commit, so the backfill
would add ~1.5G per casket of larger, less accurate duplicates (the same
reasoning that rejected symlinking Phase A into b-operand). Cost would have
been ~5-10h of repack, ~82G, and 18 production mount swaps.

`scripts/report-submodule-gaps.py` records the gap instead: 858 empty dirs
across 79 mounts, 181 of which name a repo another casket carries, written to
`docs/submodule-gaps.{md,tsv}` with an `elsewhere` column
(`<mount>:by-repo/<alias>`) so a tracer is sent to the right casket rather
than concluding the code was never collected. It is read-only and takes ~5
min over the fleet; re-run it after a rebuild. Tests:
`tests/test_submodule_gaps.py`. Note the gap is not catalog-only — Phase A
patch mounts show 2-4 empty dirs each (`driver-toolkit` →
`openshift-psap/kmods-via-containers`, `cluster-node-tuning-operator` →
`redhat-performance/tuned`), and the operators caskets still carry 6-9 each:
the stable 422 class that no API call can recover.

### Backfill sizing and rollout numbers

Backfill into already-built caskets without re-fetching component source:
`scripts/repackage-add-submodules.sh -m <mount> -o <out.sqfs.xz>` overlays the
fetched trees on the read-only mount and re-runs mksquashfs (same trick as
repackage-add-deps.sh). Overlayfs **merges** a plain upper dir with its lower
counterpart, which is exactly why the files land inside the existing empty
dirs with no whiteout needed.

`scripts/backfill-submodules-operators.sh` is the driver used for the first
rollout. **Backfilled 2026-08-19**: the five Phase B operators caskets
(`b-4.18`-`b-4.22`) as `casket-20260819-ocp<minor>-operators.sqfs.xz`.
Those artifacts have since been superseded — submodule collection now runs in
the normal pipeline, so the 2026-08-28/29 auto-update built it in from the
start, and that is what went live on 09-01. The backfill numbers are still the
ones to reason about when sizing a repack: 327/327 submodule dirs filled, 269
exact / 58 APPROX, ~16 min per casket (the repack, not the fetching,
dominates) and **+233-258 MB compressed each** — the whole fix costs about a
tenth of what the deps layer did. Nested rounds mattered: 44 of the 327 came
from depth 2-3 (`jaeger/idl/opentelemetry-proto`,
`cloud-api-adaptor/podvm-payload/kata-containers`), all exact, and no depth-4
chain exists in this set.

Two things this rollout settled:
- The APPROX rows are **not** a token or API problem. `git/trees/<sha>` answers
  **422 "Invalid object requested. SHA must identify a commit or a tree."**
  (and 404 without `recursive=1`) for `openshift/loki`,
  `openshift/ocp-release-operator-sdk`, `apache/camel-k` — the labelled commit
  simply is not on public github, the same konflux-internal-SHA class as the
  b-operand gap above. No API call can recover it; the branch head is the only
  answer and `exact=0` is the honest record. It is a stable ~11-12 per casket.
- **4.21 was never affected, and not because it was fixed.** Its INDEX row
  resolves to `openshift/zero-trust-workload-identity-manager` *directly* with
  an empty ref (dir `...-head`, a branch-head fetch), not to the `-release`
  wrapper the other four use, so the operator code was already at the top
  level. Verifying with a path pattern that assumes the `-release` layout
  reports a false zero for it — check which repo the minor actually resolved
  to before concluding a casket is empty.


## OpenGrok

### keep-patches cap and the variant-key trap

(2026-07-29) `config/opengrok-minors.txt` carries a `keep-patches: N` cap that
`stage-sources.sh` applies to the patch-scoped projects (`ocp-<ver>`,
`srpms-<ver>`); **production runs `keep-patches: 1`** — only the newest
z-stream of each indexed minor is in OpenGrok. Phase A's swap is additive by
design — a patch bump is a new mountpoint and the old patch stays `live`
forever, and `casket-cleanup.sh` only ever touches `retired` entries — so
nothing was bounding the OpenGrok project list. Measured cost of one
`ocp-<patch>` project: 19G index + 14G xref = **~33G of SSD**, against 2.6G
of HDD for the casket itself; at ~1-2 z-streams/month across 3 indexed minors
that was ~100-200G of SSD per month against 648G free. Capped-out patches stay
mounted and stay searchable via ripgrep/casket-mcp — only the index skips them,
so patch-to-patch diffing within one minor now goes through casket-mcp instead
of the web UI. Dropping a patch orphans its `/srv/opengrok-data/{index,xref}/
<proj>`; `stage-sources.sh` reports those and deletes them with
`--prune-index`. Pure logic in `opengrok/scripts/lib-stage.sh`, tests in
`tests/test_stage.sh` (the trap: `sort -V`, not lexical — 4.22.10 outranks
4.22.5, and a lexical sort would drop the newest z-stream).

**(2026-08-13) The cap key is `<minor><-variant>`, not the bare minor.** a-rpm
publishes the node-OS extensions layer as its own tree,
`by-ocp/<patch>-extensions/`, so `stage-sources.sh` sees two versions that
share a minor and are *not* z-streams of each other: `4.20.32` is the
568-package default-install set, `4.20.32-extensions` the 67-package layer on
top. Grouped under the bare minor they compete, and `sort -V` ranks
`4.20.32-extensions` **above** `4.20.32` — so production's `keep-patches: 1`
kept the extensions tree and silently dropped the base one. kata would have
been indexed at the price of every default-installed package. Each track now
keeps its own newest N (`track_of` exposes the same key for the skip log,
which until then printed a minor that was not what the decision was made on).
Any future variant (`-el10`) inherits this for free.

### Never rm -rf the staging tree while the container is up

(2026-07-30) `/srv/opengrok-src` is bind-mounted into `casket-ocp-grok`, and a
bind mount resolves to an **inode**, not a path — `rm -rf $STAGE_DIR && mkdir`
leaves the container looking at the deleted inode, `/opengrok/src` reads as
empty inside, every project disappears from the webapp and **every**
`/xref/...` 404s, including fully-indexed projects nobody touched.
`stage-sources.sh` now clears the children and keeps the directory. The index
under `/srv/opengrok-data` is never affected, which is what makes this so easy
to misdiagnose as "not indexed yet" (it was, for an hour). Tell them apart with
`podman exec casket-ocp-grok ls /opengrok/src | wc -l` against the host count;
`/xref/` answering 200 while `/xref/<known-indexed-project>/` 404s means an
empty project list, not a missing index. Recovery is a container recreate:
`SKIP_STAGE=1 WORKERS=4 opengrok/scripts/run-opengrok.sh` — run it under
`systemd-run`, because the script does `podman rm -f` before `podman run` and
losing the shell in between leaves no container at all.

**(2026-08-13) That means `systemd-run --user`, never `sudo systemd-run`.**
SELinux is Enforcing here and a system unit runs as `init_t`, which may not
execute a script labelled `user_home_t` — anything under `/home` dies at
`status=203/EXEC` with `avc: denied { execute }` before a single line runs.
The same applies to `--property=StandardOutput=file:` pointing anywhere under
`/tmp` (`209/STDOUT`, `user_tmp_t`); let it go to the journal instead. The
working form for a root-needing script is to keep the unit in the user manager
and put sudo *inside* it:

    systemd-run --user --unit=casket-stage --collect \
      /usr/bin/sudo -n /home/<user>/ocp-source-collector/opengrok/scripts/stage-sources.sh

Read its output from the **system** journal, not `journalctl --user` — sudo
re-execs as root, so the lines land with `_UID=0`. `run-opengrok.sh` itself
needs no sudo (podman here is rootless, owned by the invoking user), so for
that one a plain `systemd-run --user` is enough. Both failure modes abort
*before* the script starts, so a botched invocation is harmless — but it looks
exactly like the script failing.

**(2026-09-15) The same rule governs *installed* system units, not just
`systemd-run` — and it cost a two-day outage.** `casket-mounts.service` lives
in `/etc/systemd/system/` and named its script directly
(`ExecStart=/home/<user>/ocp-source-collector/scripts/casket-mounts.sh
--apply`). It had been running fine through the 2026-09-11 boot, so nobody
connected it to the 2026-08-13 note above. Which label let `init_t` exec it
until then is not recoverable after the fact — what is certain is that a
throwaway `podman run -v ~/ocp-source-collector:…:Z` relabelled the whole tree
to `container_file_t:s0:c405,c705` (no `:Z` appears anywhere in this repo's own
scripts, and no surviving container carries that MCS pair), and that from the
next boot the unit failed `203/EXEC` every time. The blast radius is much wider than
the unit: **0 of 87 squashfs mounted**, so `/srv/sources-*` were all empty,
`casket-mcp` answered on :8765 while serving nothing, and `opengrok.service`
spun in `activating (start-pre)` for 1356 restarts because its `ExecStartPre`
polls `casket-mounts.service` — taking `:8080` `/xref/` down with it.

**`restorecon` alone does NOT fix this, and the second failure looks identical.**
`restorecon -RF` restores the policy default `user_home_t`, which `init_t` also
may not execute, so the service fails with the same `203/EXEC`. Do not read
that as "the relabel didn't take" — check the AVC's `tcontext`, which changes
from `container_file_t` to `user_home_t`. The actual fix needs no SELinux policy
change at all: `init_t` *may* read `user_home_t`, so exec `bin_t` and pass the
script as an argument —

    ExecStart=/usr/bin/bash /home/<user>/ocp-source-collector/scripts/casket-mounts.sh --apply

Patch it in **both** `systemd/casket-mounts.service` and the sed in
`scripts/install-mounts-service.sh`: the installer rewrites `ExecStart` wholesale,
so fixing only the installed unit is silently reverted by the next reinstall.
`tests/test_config_syntax.sh` now fails the build if a system unit in `systemd/`
execs anything outside `/usr`, `/bin` or `/sbin`, or if the installer drops the
interpreter. User units are exempt — they run in the user's own domain, not
`init_t`, which is why `casket-auto-update.service` may name its `%h` script
directly.

### Symlink cycles hang the indexer silently, forever

(2026-08-08) OpenGrok's post-index cleanup — `IndexDatabase.finishWriting`
→ `PendingFileCompleter.completeDeletions` → `tryDeleteParents` →
`findFilelessChildren` — walks with plain `java.io.File`: `isFile()` follows
symlinks, anything else is recursed into, and there is **no cycle check and no
depth limit**. It logs nothing while doing this. Presentation: one
`opengrok-reindex-project` left running, one thread at ~94% CPU, no new xref
files for hours, `Sync starting` with no `Sync done`. It looks exactly like a
slow index and it never completes — the job has to be killed by hand (kill the
indexer JVM by explicit PID; `pkill -f` matches your own shell here). Note this
only fires on a **re-index that has deletions**: `tryDeleteParents` runs over
the parents of deleted xref files, so a first-time full index never trips it.

Diagnosis that actually separates hung from slow, in order: thread CPU vs
process CPU from `jstack` (`cpu=` per thread) tells you whether one thread is
burning everything; GC thread CPU rules out a heap death-spiral (here G1 Conc
was ~32s against 2h47m in the walker, so GC was innocent despite a 97%-full
8G heap); then sample the recursion depth twice — real source trees bottom out
around 25, and 51 → 52 over several minutes means unbounded descent, not work.

The 4.22 culprit was **fwupd-2.0.19's mock sysfs fixture** (`src/tests/sys/`):
`sys/class/<cls>/<dev>` → `../../devices/.../<dev>`, whose `subsystem` link
points back at `../../../class/<cls>`. Visited dirs per depth grew 1.72x per
level (396 → 10151 between depth 15 and 21); at the depth 52 the JVM had
reached, that level alone is ~4.7e11 directory visits. criu's `compel → .` is
a real self-referential link in the same casket but is **not** the cause —
the indexer rejects it, so it never reaches xref. Only 4.22 was affected;
4.18/4.20 carry older fwupd without the fixture.

**fwupd was not the only one.** A full scan of all 18 staged projects the same
day found **54 cycles across 12 of them**, every one a landmine waiting for a
re-index with deletions in that subtree: `testdata → .` in systemd (all three
`srpms-*`), `compel/include/uapi/compel → .` in criu, hypershift vendoring
itself at `hack/workspace/hypershift` (three `ocp-*` plus `layered-*/mce`),
flux's and kn-plugin-func's deliberate symlink test fixtures, and
sriov-network-operator's `.gopath` GOPATH shim. Do not assume a project that
has indexed cleanly for months is clear — it only means nothing has been
deleted under its cycle yet.

Two guards, because the ignore list alone is whack-a-mole:
`config/opengrok-ignore.txt` is fed to the indexer as `-i <pattern>` by
`run-opengrok.sh` — 7 patterns cover all 54, verified by replaying
`Filter.addPattern`'s semantics over the scan output (a match on the link *or
any ancestor* is enough, since the indexer then never descends). Prefer the
glob form anchored on the component name (`d:*/systemd-*/test/testdata`) over
a bare suffix: it is version-proof across z-stream bumps and cannot collide
with an unrelated project's `test/testdata`. And
`opengrok/scripts/find-symlink-cycles.py` scans the staged tree for cycles at
the end of every `stage-sources.sh` run (ancestor-inode match, plus a global
visited set — without it the scan hangs exactly like the indexer it protects;
~15 min over the full tree, `SKIP_CYCLE_CHECK=1` to skip; tests in
`tests/test_stage.sh`). It reports, it does not edit the ignore list — which
source stops being browsable is a judgement call. Removing an already-poisoned
project's xref subtree by hand is part of the fix: the ignore stops the
indexer entering the tree, but a cleanup walk over xref that is already cyclic
hangs the same way.

Two numbers worth having straight: a **re-sync of all 18 projects after a
source change takes ~6 minutes**, not the ~10h quoted elsewhere (that is
a from-scratch build of the whole index). And the indexer logs
`markProjectIndexed ... 401 Unauthorized` on every run; searches still work as
long as `configuration.xml` was saved, but that is the same warning behind the
silent 0-hit failure, so after any restart verify a real query returns hits
rather than trusting an xref 200.

### --disableRepository only governs detection, and the persisted config outlives it

(2026-09-12) The search page listed four unreachable CVS repositories —
`srpms-4.20.32-extensions` and `srpms-4.22.8-extensions`, each with
`telnet/CVS` and `libtelnet/CVS`, all showing N/A for parent branch and
current version. They are real: the RHEL telnet SRPM extracts a 1998 checkout
of netkit-telnet with its CVS metadata intact (`Root =
:ext:jbj@devserv:/mnt/devel/CVS`, a Red Hat internal host long gone), and
OpenGrok registers any `CVS/` directory as a repository. telnet reaches the
node only through the extensions layer, which is why neither base `srpms-*`
project shows it.

Adding `--disableRepository CVS` to `INDEXER_OPT` is necessary but **does
nothing on a host that already has them**. Two separate facts:
- The flag stops the indexer *detecting* a repository. It does not remove one
  the webapp already knows about, and `opengrok-reindex-project` (the
  per-project sync `start.py` actually runs) never re-scans the repository list
  nor writes `disabledRepositories` back to the webapp. The indexer's own log
  says `Done invalidating repositories (0 valid, 0 working)` while the webapp
  keeps serving all four — the two configurations are simply different.
- `configuration.xml` lives in `ETC_VOLUME`, so the bare-config generation that
  *would* apply the flag (`create_bare_config`, which passes `INDEXER_OPT`
  alongside `-S -W <config>`) only ever runs when that file is absent. A
  container recreate does not re-run it. **So the committed flag is the whole
  fix on a fresh host, and no fix at all on this one.**

The cheap repair is one REST call against the running webapp (token in
`/srv/opengrok-etc/webapp_api_token`), then persisting the result the same way
`start.py`'s `save_config()` does — `GET /api/v1/configuration` written to the
file. No reindex, no restart, so the 0-hit startup race is never in play:

    T=$(sudo cat /srv/opengrok-etc/webapp_api_token)
    curl -X PUT -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
      -d '["GitRepository","PerforceRepository","CVSRepository"]' \
      http://localhost:8080/api/v1/configuration/disabledRepositories
    curl -H "Authorization: Bearer $T" http://localhost:8080/api/v1/configuration \
      > /tmp/c.xml    # verify, then install over /srv/opengrok-etc/configuration.xml

Setting `disabledRepositories` alone is enough — applying the configuration
re-validates the repository list, the disabled type fails to instantiate
(`Failed to instantiate internal repository data for CVS in ...`) and the
entries drop out. `PUT .../configuration/repositories` with `[]` is not needed.
Verify before installing the file: well-formed XML, `CVSRepository` present,
the offending paths gone, and the **same Project count as the file it replaces**
(21 here) — a config that silently loses projects is the 0-hit failure by
another road. Back the old one up (`configuration.xml.bak-<date>`); the next
sync's `save_config()` rewrites the file from webapp memory, which now agrees.
The source stays fully searchable throughout — `full:telnet` still returned 348
hits in `srpms-4.22.8-extensions` after the change; only the repository
registration went away.


## B-operand source coverage

### Empty git/ causes and resolution strategies

(2026-07-30) Reported as "OpenGrok isn't indexing
layered-4.18/service-registry-operator"; it was never an indexing bug. 97 of
146 layered-4.18 products (97/149 in 4.20, 89/138 in 4.22) had an **empty
`git/`**, and `stage-sources.sh` created the project dir anyway, so
`/xref/<proj>/<name>/` 404'd. Three distinct causes, and only the first is
"no source exists":
- 39 products: no source-ish label on any image (`z:none`) — ISV images.
- 49: resolved to `openshift/*` but the labelled commit 404s on public github
  (internal konflux SHA). **Do not assume Phase A covers these** — of the 82
  unfetchable repos only **17** are in the payload; metallb, sriov-\*, velero,
  ViaQ/\*, migtools/\* are layered-only.
- 9: same, on non-openshift repos (migtools, open-telemetry, ViaQ).

Fixes: `stage-sources.sh` writes a `SOURCE-NOT-COLLECTED.txt` naming the
per-component reason (and the `ocp-<patch>` project when the payload happens to
have that repo) instead of an empty dir — symlinking the Phase A copies in was
rejected at ~20G of SSD per minor for 1.6G of shared infra source.
`phase-b-operand-fetch-source.sh` now walks `candidate_source_urls` instead of
just sha→`v<ver>` (probe: 156/156 unfetchable 4.18 repos get *something*), but
155 of those 156 only answer on a **branch head**, so every non-exact hit is
logged `APPROX` and recorded in `meta/git-fetched.tsv` (`exact=0`) with
MANIFEST totals split `source_exact`/`source_approx`/`source_fetch_failed`.
A resolved count alone is not coverage. New in `lib-resolve.sh`: the ".0
release of that MAJ.MIN" tag (KMM 2.6→v2.6.0, otel 0.152.1→v0.152.0),
`release-<product-minor>` branches (MTC 1.8.15→release-1.8), Apicurio's
`<ver>.Final`. New in the resolver: `INFRA_REPOS` rejects
`konflux-ci/mintmaker` — all four KMM images label the build bot as their
source, and rule (c) was happily accepting it (method `z:infra-label`, never a
silent drop); `tag-csv` mode takes the version from the operator's CSV for
images carrying no version label at all (apicurio). 22 z:none components
mapped, each verified by probing the chain; products whose only hit was
main/master (amq-broker, strimzi, noobaa, 3scale, Quay, Kuadrant
sub-components) are left unmapped **on purpose** and the reason is in the code.

**Deployed 2026-08-01**: all 9 minors rebuilt and swapped live. Products with
real source per minor: 4.18 **49 → 121**/146, 4.20 52 → 120/149, 4.22 49 →
111/140. For 4.18's 1355 operand images, 769 resolve and **767 now have a
tree** (457 exact, 310 approximate, 2 unfetchable) where before nearly every
rescued row failed to download. The rebuild averaged ~1.5h/minor, not the
~8h/minor first guessed from an early sample — most of a minor is SKIPs of
already-fetched tarballs. Remaining gaps are the honest ones: 25 products in
4.18 still have no public source at all.

### Phase B resolution overhaul (2026-06-09)

The "empty `ref` but valid `csv_repo`" NO_SOURCE cases were three independent
bugs, now fixed — 4.18 jumps from 34 → 110 unique source dirs. See
`docs/archive/phase-b-uncollected-plan.md`:
- `phase-b-fetch-bundles.sh` no longer drops operators whose CSV lacks a
  `containerImage` annotation (was excluding ~30/122 incl.
  serverless/pipelines/compliance).
- `phase-b-resolve-v2.sh` adds per-repo ref strategies (gitops `v<MAJ.MIN>.0`,
  maistra `maistra-<ver>-dev`, serverless `release-<product-minor>`, compliance
  branch) and fixes the empty-ref bug (NOT xargs — `IFS=$'\t' read` collapses
  consecutive tabs, blanking fname; guarded with a `_NONE_` sentinel).
- `phase-b-package.sh` prefers `git-v2.tsv` for meta/MANIFEST/INDEX so
  by-component/by-repo cover the rescued operators.
- Pure resolution logic extracted to `scripts/lib-resolve.sh` with regression
  tests (`tests/`, run in CI), and `dl_one` walks `candidate_source_urls` from
  there (`phase-b-resolve-v2.sh:210`).

Structural limit (unchanged): per-minor non-github CSV repos
(`access.redhat.com/containers/...`, `www.redhat.com`) can't be rescued. Two
remaining hard cases need version mapping: pipelines (product v1.22 ↔ tektoncd
v0.79) and compliance (RH ahead of public tags).


## Build and storage

### Accumulated stages fill the root fs (ENOSPC)

(2026-08-09) `casket-build.sh` now deletes each unit's `50-out/` after
registering the artifact. It never used to, and on 2026-08-09 the **286
accumulated stages came to ~666G and filled the root fs** — 320K free. Seven
b-operand minors then failed with ENOSPC in seconds each, and the trailing
`run-opengrok.sh` died mid-restage (`ln: … No space left on device`), leaving
OpenGrok with 17 of 18 projects and one of them 82/366 complete. Three things
compounded, and the first is the one that made an old habit suddenly fatal:

- **deps collection (2026-07-26) took a stage from a few GB to ~33G.**
  `$CASKET_DEP_STORE` is on the HDD while the work tree is on root, so
  collect-deps' store→stage hardlinks **cannot span the two filesystems** and
  degrade to full copies. Every stage carries its own complete copy of that
  minor's deps. (This is the same cross-fs trap as the dep-store section
  above, from the other direction: there the store landed on the wrong fs,
  here the stage does.)
- b-operand stages **one per product per minor** — ~190 dirs for a single minor.
- Phase A never retires an old patch, so each z-stream adds a whole new
  `ocp<patch>/` work dir; the weekly auto-update piles them up by count too.

**b-operand has a second stage that is easy to miss, and it is a hardlink
farm.** `phase-b-operand-combine.sh` assembles
`phase-b-operand/_layered/<minor>/stage` with `cp -al` — hardlinks into each
product's `50-out/stage`. It is *not* named `50-out`, so a `find -name 50-out`
sweep walks straight past it, and while the per-product stages still exist it
shows up as costing almost nothing. Delete the `50-out` dirs and the `_layered`
links silently become the **sole owners** of that data: the space is never
returned. Measured 2026-08-09: three minors' `_layered` held **409G** (4.14
123G, 4.15 132G, 4.16 154G) after their `50-out` dirs had been reclaimed —
the first cut of this fix freed only 162 per-product dirs and got 161G less
back than expected, which is how it was found. `combine.sh` `rm -rf`s only the
minor it is about to rebuild, so every other minor's copy persists forever.
`casket-build.sh` therefore passes `_layered/<minor>` to `reclaim` in the same
call as the per-product glob.

Only staging output is removed. `00-discover/`, `10-git/`, `20-git/` and
`40-manifest/` stay, so a rebuild re-stages from tarballs already on disk and
**re-downloads nothing** — the whole b-operand tarball corpus is only ~18G, so
keeping it is cheap and re-fetching it is not. The guard deciding what may be
`rm -rf`'d lives in `scripts/lib-reclaim.sh` with tests in
`tests/test_reclaim.sh`; the trap it encodes is that in a bash `[[ ]]` pattern
**`*` matches slashes**, so `"$root"/*/50-out` alone happily accepts
`$root/../../elsewhere/50-out` — hence the separate `..` check.
`--keep-stage` / `CASKET_KEEP_STAGE=1` opts out when inspecting a build.

Cleaning up by hand after an older run: `find $CASKET_WORK -type d -name 50-out`
then remove, but **check no build is running first**
(`pgrep -f 'casket-build.sh|collect-deps.py|mksquashfs'`) — deleting a live
stage corrupts the in-flight build.

### Thin b-operand casket does not announce itself

(2026-08-24) `phase-b-operand-combine.sh` validates only
`[[ ${#included[@]} -gt 0 ]]`, and `casket-build.sh`'s per-product loop
deliberately skips any product whose discover/fetch-source/package step failed —
that skip is right (a package can be absent from an older minor's catalog) but
it means a disk-full or a transient registry outage produces a **thinner casket
that still builds, still registers as staged, and still looks fine**. The
2026-08-09 ENOSPC event is exactly the shape that would do it.
`scripts/check-layered-completeness.sh` is the gate: it reads
`Products included:` out of each artifact's own `README.txt` (via
`unsquashfs -cat`, one small file rather than a listing of a 13G image) and
diffs the staged artifact against the live one it would replace. Run it before
every b-operand swap. Measured 2026-08-24 across all 9 minors:
124/126/162/138/146/143/149/142/141 products, zero lost, 4.22 +1 new. A raw
count against the 191-entry catalog is NOT the check — the catalog is an upper
bound and legitimate per-minor skips vary from 29 to 67.

### Artifact name date mismatch across UTC midnight

(2026-08-21) `casket-build.sh` used to re-derive each packaging script's
date-stamped output name with a second `date -u`. Both sides were UTC, so this
is *not* the local-vs-UTC bug `package.sh` already carries a comment about — it
is the same expression evaluated hours apart. A build long enough to straddle a
UTC midnight makes them disagree and `register` dies with "expected artifact not
found" on a casket that built perfectly: b-operand 4.19 on 2026-08-16 built for
8h50m, produced `casket-20260815-layered-ocp4.19.sqfs.xz`, was looked for as
`...20260816...`, and 13G sat unregistered until 08-21. b-operand runs 3-9h per
minor, so this is reachable any week. Packaging scripts now take
`--artifact-out FILE` and call `record_artifact`; `casket-build.sh` reads it
back with `artifact_path_from`, falling back to its own guess so a manual run
without the flag behaves as before. Both helpers are in `lib.sh`, tests in
`tests/test_artifact_path.sh`. The trap worth remembering: `read` returns
non-zero at EOF without a trailing newline **but has already filled the
variable**, so `|| p=""` there re-creates the very bug the helper prevents.

### Freshness check: exact vs drifting modes

(2026-08-11) `b`/`b-operand` are fingerprinted by the redhat-operator-index
manifest digest, and upstream rebuilds that roughly daily, so plain equality
reported STALE essentially forever: on 2026-08-10 it flagged 18 rows, and
**seven of the nine minors it called stale had been built that same day** —
4.14–4.17 went `fresh` → `STALE` within one hour with no build in between. A
check that is always red cannot tell you anything on the day something is
actually wrong.

Two verdict modes now, in `scripts/lib-freshness.sh` (tests:
`tests/test_freshness.sh`, wired into CI):
- **exact** (`a`, `a-rpm`) — the fingerprint is a release version, so it only
  moves when there is real new content: any difference is STALE at once, and
  age is irrelevant. A minor that has not shipped a z-stream in two months is
  quiet, not stale.
- **drifting** (`b`, `b-operand`) — a moved digest prints `drifted` and exits
  0. It becomes STALE only once the live artifact is *also* older than
  `CASKET_STALE_AFTER_DAYS` (default 28, `--stale-after N`). A *matching*
  digest is never stale however old, because there is nothing to rebuild
  toward.

The window is sized against the auto-update cadence and the two numbers are
one decision: a window at or below the cadence puts the check back to
permanently red, which is the failure this whole mode exists to fix. At the
weekly cadence it was 10 days (healthy ≈7d, one missed run ≈14d). Since the
cadence moved to **3 weeks on 2026-09-12** it is 28: a healthy cycle leaves
the live artifact at most 21d + the build itself (~3d) old, a missed run
reaches ~42d.

### Shell gotchas

`awk '{print $2; exit}'` piped from `curl` breaks under `set -o pipefail` —
the early `exit` closes the pipe while curl is still writing, so curl exits 23
(SIGPIPE) and pipefail propagates that as a failure even though the value was
captured correctly. `lib-fingerprint.sh`'s `fetch_patch` drops the `exit` for
this reason (release.txt has exactly one `Version:` line, so it's safe to just
not early-exit).

Bash's `${var//#/repl}` does **not** treat a bare `#` as a literal in the
pattern position — even in the `//` (global) form, `#` is parsed as the
start-anchor operator, so `${cur//#/\\#}` (the escaping idiom in the older
`swap-*.sh` scripts, meant to escape `#` for sed's `#`-delimited pattern)
silently no-ops instead of escaping anything. `casket-swap.sh` avoids the whole
problem by rewriting the fstab line with an `awk` exact-field match instead
of a sed regex substitution.
