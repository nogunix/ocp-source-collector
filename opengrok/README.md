# ocp-source-collector OpenGrok source browser

An [OpenGrok](https://oracle.github.io/opengrok/) container that lets you
search symbols, cross-references, and full text across the collected OCP
release sources (Phase A / A-rpm / B / B-operand + certified/community catalogs)
from a web browser. Uses the official `docker.io/opengrok/docker` image with
podman as-is (no custom build required).

## Architecture (~54 projects)

```
/srv/sources-*              casket squashfs mounts (read-only)  <- input
        |  stage-sources.sh generates a symlink tree
        v
/srv/opengrok-src/          clean symlink tree
  ├── ocp-<ver>/<comp>       -> /srv/sources-ocp<ver>/git/<comp>-<sha>              (A x9: 4.14-4.22)
  ├── operators-<minor>/..   -> /srv/sources-ocp<minor>-operators/git/..            (B x8+)
  ├── certified-<minor>/..   -> /srv/sources-ocp<minor>-certified-operators/git/..  (certified x9)
  ├── community-<minor>/..   -> /srv/sources-ocp<minor>-community-operators/git/..  (community x9)
  ├── layered-<minor>/<prod> -> /srv/sources-layered-ocp<minor>/<prod>/git/..       (B-operand x8)
  └── srpms-<ver>/<pkg>      -> /srv/sources-ocp-srpms/srpms/<NEVR>                 (A-rpm, one project per OCP version)
        |  run-opengrok.sh mounts staging + original sources and starts the container
        v
podman container casket-ocp-grok   :8080  OpenGrok Web UI  * rootless (regular user that owns the repo) -- do NOT run with sudo
  index persisted to /srv/opengrok-data (SSD, bind mount, ~150-250G). HDD is impractical
  (xref = one tiny .gz per source file, index = millions of random inode accesses, inode-bound;
  HDD makes indexing/searching unusable). Only minors in config/opengrok-minors.txt are indexed on SSD.
```

> Phase names reflect the 2026-07-11 rename (A-rpm = formerly B, B = formerly C, B-operand = formerly D).
> layered (B-operand) projects expand into `layered-<minor>/<product>/<clean-name>`.
> certified/community catalogs added 2026-07-11 --
> `stage-sources.sh` detects `-certified-operators` / `-community-operators` mounts
> and routes them to `certified-<minor>` / `community-<minor>` projects.

> **z-stream index cap (2026-07-29)**: `config/opengrok-minors.txt`'s
> `keep-patches: N` (production = **1** = only the newest z-stream per minor)
> limits patch-scoped projects (`ocp-<ver>`, `srpms-<ver>`). Phase A's swap is
> additive -- old patch mounts stay `live` permanently (seeing past patches
> accurately is a core value of the caskets), and `casket-cleanup.sh` only
> deletes `retired` entries, so without a cap the project count grows without
> bound. Measured: one `ocp-<patch>` project = index 19G + xref 14G =
> **~33G SSD** (the casket itself is 2.6G on HDD). Capped-out patches stay
> mounted and remain searchable via ripgrep / casket-mcp (only the index is
> skipped). Orphaned `index`/`xref` from dropped patches are cleaned up with
> `--prune-index`.

## Scripts (`scripts/`)

| Script | Role |
|--------|------|
| `stage-sources.sh`  | Generate a clean symlink tree from `/srv/sources-*` to `/srv/opengrok-src/` (needs root, sudo automatic). `--prune-index` deletes `index`/`xref` for projects no longer in staging |
| `run-opengrok.sh`   | Refresh staging, mount source + index/etc volumes, and start the container. Disables suggester nightly rebuild after startup |
| `stop-opengrok.sh`  | Stop and remove the container (index volume kept by default; `--wipe-index` to delete) |
| `entrypoint-ro.sh`  | Custom entrypoint for read-only squashfs (bypasses the official image's `chown -R`) |
| `package-airgap.sh` | Create an airgap distribution (image + index + scripts) |
| `deploy-airgap.sh`  | Deploy OpenGrok from the airgap distribution in a production environment |

## Daily operations

```bash
# Start (regenerate staging + start container). First run indexes in the background.
# Always run rootless (regular user) -- sudo causes podman to not find the image
# in rootless storage and attempt to pull from docker.io, hitting rate limits (2026-07-11 incident).
# stage-sources.sh uses sudo internally, so run-opengrok.sh itself needs no sudo.
./scripts/run-opengrok.sh

# Monitor progress
podman logs -f casket-ocp-grok          # "Sync done" = complete

# Stop (index is kept -> no re-indexing needed next time)
./scripts/stop-opengrok.sh

# Access
#   http://<host>:8080/                  top page (search)
#   http://<host>:8080/xref/<project>/   tree browsing
# Open the firewall for LAN access:
#   sudo firewall-cmd --add-port=8080/tcp [--permanent && sudo firewall-cmd --reload]
```

Tune via environment variables (example): `PORT=8888 INDEXER_JAVA_OPTS=-Xmx12g ./scripts/run-opengrok.sh`.
After adding/replacing sources, re-run `./scripts/run-opengrok.sh` (incremental re-index).

**Memory settings (reference host: 60G RAM, ~215G index, ~54 projects -- important)**:

| env | Default | Role | Recommended |
|-----|---------|------|-------------|
| `INDEXER_JAVA_OPTS` | `-Xmx8g` | Heap for each reindex JVM. At startup, one JVM per project is launched **in parallel** | **`-Xmx2g`**. `-Xmx8g` x 25 parallel exceeds RAM and causes `pthread_create EAGAIN`, corrupting some project indexes |
| `CATALINA_OPTS` | (empty = default ~ 25% RAM ~ 15G) | Webapp JVM heap. Used for loading full-text indexes, suggester rebuild, and search result sets | **`-Xmx24g` to `-Xmx32g`**. The default 15G causes heap exhaustion on suggester rebuild or large queries, leading to GC thrashing and webapp hangs |
| `WORKERS` | `4` (set by `run-opengrok.sh`; the image's own default is `nproc`=16) | **Parallel project count** during startup sync. One JVM is spawned per project | Default `4` is low enough. Only raise it when the host is idle and you want faster completion (`WORKERS=8` etc.) |

Recommended update command: `SKIP_STAGE=1 INDEXER_JAVA_OPTS=-Xmx2g CATALINA_OPTS=-Xmx32g ./scripts/run-opengrok.sh`
(`SKIP_STAGE=1` = reuse the existing symlink tree without regenerating. `WORKERS` defaults to `4` in `run-opengrok.sh`, so it normally needs no override).

**Root cause of the load (found 2026-07-02)**: The container is recreated (`podman rm -f` + `run`) every time `run-opengrok.sh` runs (and after every host reboot), triggering a resync of all ~54 projects with up to `WORKERS` in parallel (one JVM per project). With the default `WORKERS`=`nproc` (16 on the reference host), 16 JVMs compete for CPU/IO simultaneously, making the host noticeably sluggish. `INDEXER_JAVA_OPTS`/`CATALINA_OPTS` address memory exhaustion (EAGAIN / GC thrashing), not this CPU contention. `run-opengrok.sh` defaults `WORKERS=4` to mitigate. Index data itself is persisted on volumes and only incremental, so lowering parallelism doesn't affect per-project time -- it just reduces concurrency and peak load at the cost of longer total time.

> **Note -- when only operator content has been updated**: `run-opengrok.sh` recreates the container,
> which resets `/opengrok/etc/configuration.xml` and triggers a **parallel** re-index of all projects
> at startup. Index data persists on `/srv/opengrok-data` (SSD bind mount) so incremental is fast,
> but incorrect heap settings can cause EAGAIN / GC thrashing. To update only a few projects, a
> single-JVM serial index is safer:
> ```bash
> podman exec -u appuser casket-ocp-grok java -Xmx8g -jar /opengrok/lib/opengrok.jar \
>   -c /usr/local/bin/ctags -s /opengrok/src -d /opengrok/data \
>   -P -H -G -r dirbased -m 256 --leadingWildCards on \
>   --disableRepository Perforce --disableRepository git --canonicalRoot /srv/ \
>   -W /opengrok/etc/configuration.xml -U http://localhost:8080 \
>   --token @/opengrok/etc/webapp_api_token
> ```
> Also, **full-text queries on ultra-frequent words** like `/api/v1/search?full=the` return
> hundreds of thousands of results and exhaust the webapp heap. Always test with specific symbol
> names or unique terms (normal MCP searches are unaffected).

### Required startup options (set automatically by run-opengrok.sh)

Non-obvious workarounds needed to feed read-only squashfs to OpenGrok:

- `--security-opt label=disable` -- sources are SELinux `user_home_t`; squashfs cannot be relabeled
- Custom `entrypoint-ro.sh` -- bypasses the official entrypoint's `chown -R` which fails on read-only mounts
- `INDEXER_OPT="--disableRepository Perforce --disableRepository git --canonicalRoot /srv/"`
  - `Perforce`: stops an 8-second p4 hang probe in every directory
  - `git`: stops false detection of embedded git-like metadata (ko's `kodata/HEAD`, libssh's `.git` in SRPMs)
    which causes history cache failures and empties the project (we don't need history)
  - `--canonicalRoot /srv/`: lets OpenGrok follow staging symlinks (which point to /srv)

### Startup race workaround -- silent 0-hit search bug (found 2026-07-12)

**Symptom**: xref (tree browsing) works perfectly, but full-text and symbol searches
return **0 results for everything**. `podman logs` shows
`IndexNotFoundException: no segments* file found in MMapDirectory@/opengrok/data/index`
repeatedly (listing all project names). Easy to miss since browsing still works.

**Cause**: The container's `/scripts/start.py` runs a project sync sequence
(`sync.yml`) at startup, which first hits `POST /api/v1/messages` for each project.
If this POST **loses the race** against Tomcat's REST layer deployment, it fails
with `Connection refused`, aborting the entire sequence before reaching the final
step that marks projects as `indexed` (`opengrok-reindex-project -U <url>`).
OpenGrok's REST search filters `ProjectHelper.getAllProjects()` to only
`isIndexed()==true` projects, so when all are `indexed=false` the filter returns
empty, falling back to the legacy `searchSingleDatabase()` path (designed for
no-project configurations). This tries to open a non-existent root-level
`/opengrok/data/index` with Lucene and throws `IndexNotFoundException` every time.
xref doesn't use this path (it reads files directly), so it's unaffected -- hence
"browsing works but search is dead".

The first full build succeeded by timing luck, so this trap only **manifests later**
after `systemctl restart opengrok` or a reboot.

**Fix**: Persist `/opengrok/etc` to `ETC_VOLUME` (see below). Once the first sync
completes and writes `indexed=true` to `configuration.xml`, it survives recreates
and reboots, so the startup race **never re-manifests** (the webapp reads the
persisted config and returns search results immediately). If you do hit the 0-hit
bug, re-run `run-opengrok.sh` (= container recreate + full sync re-run) to recover.
A dedicated `wait-for-ready.sh` used to re-trigger sync on every startup, but became
unnecessary after config persistence and was removed (2026-07-12). Suggester nightly
rebuild disabling is done in `run-opengrok.sh` right after container startup.

> **Never do this**: Manually hitting `PUT /api/v1/projects/<name>/indexed` for
> recovery is counterproductive. This endpoint forces a full suggester rebuild,
> causing a random-read storm on the index (`iostat` shows `%util` 99%, CPU > 100%)
> for tens of minutes, ultimately crashing the webapp with
> **OOM (`java.lang.OutOfMemoryError: Java heap space`)**. The correct recovery is
> re-running `run-opengrok.sh` (= re-running the sync pipeline), not manually
> setting the indexed flag.

### Avoiding full re-sync on restart (configuration.xml persistence, 2026-07-12)

Which projects are `indexed=true` is stored in `/opengrok/etc/configuration.xml`,
written by start.py's `save_config()` **after** the startup do_sync completes.
`/opengrok/etc` lives on the container filesystem, and behavior differs by restart type:

- **`systemctl restart` / reboot (= `podman start`, same container)**: `/opengrok/etc`
  survives inside the container, so if the previous sync completed, configuration.xml's
  `indexed=true` is still there and the webapp returns search results immediately on startup.
- **`run-opengrok.sh` (= `podman rm -f` + `run`, recreate)**: `/opengrok/etc`
  is destroyed -> configuration.xml starts from scratch -> search is dead until all
  projects are re-indexed. This was hit every time sources were swapped.

Fix: `run-opengrok.sh` bind-mounts `/opengrok/etc` to `ETC_VOLUME`
(default `/srv/opengrok-etc`), **persisting configuration.xml across recreates**.
This way the indexed config survives and search never drops even momentarily.

> start.py unconditionally runs one do_sync at startup (hardcoded). Config persistence
> means **search stays up**, but the background do_sync itself can't be suppressed
> without modifying the vendor's start.py (too fragile to maintain). Suggester is
> disabled, so the churn is only incremental index checks (non-blocking).
> On first run (bind target is empty), one bare-config full sync runs; after that,
> the persisted config is used.

## Airgap distribution

Ships the index (~215G) along with the image so the production environment can serve
immediately without re-indexing. **The index is version-specific to OpenGrok** -- ship
the image pinned by digest (a different version discards the index on startup).
Verified: volume export -> import into a different volume -> start with same image +
same source mounts -> search works without re-indexing.

Source caskets (`casket-*.sqfs.xz`) are not distributed generally -- this airgap
approach is a **special path for transporting a complete set to another environment
within the organization**, and sources are shipped alongside only in that case.
The production side must mount them at the **same `/srv/sources-ocp*` paths**
(the index references those paths).

### Build side

```bash
# Run after full indexing is complete. Outputs image.tar + index.tar + scripts + MANIFEST to OUT_DIR.
OUT_DIR=/mnt/hdd/casket-ocp/opengrok-airgap ./scripts/package-airgap.sh
# For a lean build (no index; production side re-indexes in ~3h) use INCLUDE_INDEX=0
```

### Production environment

```bash
# 1. Mount casket sources at /srv/sources-ocp* (standard casket procedure, fstab loop mount)
# 2. Extract the distribution and run:
./scripts/deploy-airgap.sh
#    -> podman load (image) -> import index volume -> generate staging -> start container
# 3. First startup runs incremental sync (~20-40 min I/O, no re-ctags), then serves immediately
```

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| xref (browsing) works but full-text/symbol search returns **all 0 results**, logs show `IndexNotFoundException: no segments*` | Startup race left all projects `indexed=false`. Re-run `./scripts/run-opengrok.sh` (container recreate + sync re-run). See "Startup race workaround" above. Manual recovery via `PUT .../indexed` causes OOM -- never do this |
| Container exits immediately (123/126) after startup | SELinux label or chown issue in `entrypoint-ro.sh`. Check that `--security-opt label=disable` is set |
| A project's index is empty (`indexed=false`) | False git detection from embedded git-like metadata in sources. Verify `--disableRepository git` is active |
| Indexing never finishes | Perforce probe. Verify `--disableRepository Perforce` is active |
| Content under symlinks not indexed | Check `--canonicalRoot /srv/` and that the original `/srv/sources-*` are mounted in the container |
| Production environment discards index and re-indexes | Image version mismatch. Use the image matching the MANIFEST digest |
| Management API `/api/v1/projects` returns 401 | By design (token required after webapp start). Web UI and `/api/v1/search` are public and unaffected -- browsing and searching work fine |
| Logs show `pthread_create failed (EAGAIN)`, some projects return 0 search results | RAM exhaustion from parallel reindex. Index data survives but webapp registration fails. Lower `INDEXER_JAVA_OPTS=-Xmx2g` and re-run, or use the single-JVM serial index command above |
| Webapp unresponsive (HTTP 000), `java` at high CPU with RSS pinned at heap limit | Heap exhaustion from suggester rebuild or large query -> GC thrashing. Recreate with `CATALINA_OPTS=-Xmx32g` via `SKIP_STAGE=1 ./scripts/run-opengrok.sh`. Test with specific-term queries |
| `run-opengrok.sh` tries to pull from docker.io and gets `toomanyrequests` | Running with `sudo`. The image is in rootless (user-side) podman storage and invisible to root. Run without sudo -- existing rootless containers are not broken by a sudo invocation |
