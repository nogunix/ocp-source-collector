# Operations: maintaining releases as new OpenShift versions ship

[pipeline.md](pipeline.md) covers *how to collect a single version*. This document covers the operational axis — *which versions are targeted, when to rebuild, and when to deploy to production* — driven by the target lists in `config/` and the lifecycle records in `state/registry.json` (staged → live → retired), connected by four driver scripts. The per-phase scripts like `discover.sh` remain unmodified (the only exception: `phase-b-operand-combine.sh` now reads its product list from `config/phase-b-operand-products.tsv`).

`a-rpm` and `b-operand` are sub-phases that look one level deeper into the same origin as A/B, not independent phases — the phase identifiers reflect this naming (see the "What gets collected" section in [README](../README.md)).

```
config/minors.txt                Target OCP minors for A/B/B-operand (8: 4.14-4.21)
config/phase-a-rpm-minors.txt    Target minors for A-rpm (7: 4.21 collection PAUSED)
config/phase-b-operand-products.tsv  Target products for B-operand (package<TAB>infix, 7 rows)
state/registry.json              Artifact lifecycle (managed by build/registry.py, never hand-edited)
```

Freshness signals (the value used to determine "has something changed" for each phase):
a = latest patch string from the stable channel / b and b-operand = manifest digest of `redhat-operator-index:v<minor>` (b-operand shares the same index as b) / a-rpm = sha256 of the patch strings for all target minors (a proxy for "the rhel-coreos image probably changed"). See the comments in `scripts/lib-fingerprint.sh` for details.

```bash
# 1. Freshness check (read-only, the only command safe for cron/systemd timer)
./scripts/casket-check.sh --phase a        # or a-rpm/b/b-operand/all (defaults to all)
#   a          4.20     STALE      current=4.20.28   registry=4.20.27   ← needs build
#   Exit code: non-zero if any unit is STALE/MISSING/UNREACHABLE

# 2. Build (calls existing per-phase scripts in order. Dry-run first to review the plan)
./scripts/casket-build.sh --phase a --unit 4.20              # dry-run
./scripts/casket-build.sh --phase a --unit 4.20 --apply      # execute + register as staged

# 3. Deploy to production (fstab is not used — mounts are registry-driven since 2026-07-11)
./scripts/casket-swap.sh --phase a --unit 4.20                # dry-run (shows what would change)
sudo ./scripts/casket-swap.sh --phase a --unit 4.20 --apply    # remount in-place + mark as live in registry

# 4. Clean up old artifacts (only retired artifacts older than 14 days, dry-run by default)
./scripts/casket-cleanup.sh
./scripts/casket-cleanup.sh --apply
```

**Automation boundary**: `casket-check.sh` is read-only (network queries only) and safe for automation. **build is also non-destructive** (creates new files + registers as staged in the registry, no sudo required) and safe for automation — **automated via timer since 2026-07-16** (wakes up Friday 23:30, runs **every 3 weeks**; changed from weekly on 2026-09-12; see casket-auto-update section below. Manual operation from 2026-07-12 to 07-16; automated after all phases went STALE during that period). swap/cleanup remain human-gated with explicit `--apply` — a-rpm inherently requires logging into a subscribed VM and can't be unattended, and destructive production operations follow this repository's existing convention (dry-run default + explicit apply, as in `swap-source-index.sh` etc.).

## Auto-update check + staged build (casket-auto-update)

Combines new-release detection through staged build in one flow. **Staging runs automatically every 3 weeks** (timer enabled 2026-07-16, changed from weekly to 3-weekly 2026-09-12), **swap is never automated** (human gate).

```
systemd/casket-auto-update.service      → scripts/casket-auto-update.sh --apply --min-interval-days 21
systemd/casket-auto-update.timer        Wakes up every Friday 23:30 (enabled on casket-host, 2026-07-16)
config/auto-update-phases.txt           Phases to auto-build (default: a only. b/b-operand commented out)
state/auto-update.status                Per-unit summary of latest run (.gitignore, registry.json tracked as before)
state/auto-update.last-run              Cadence stamp. mtime = last **completed** apply run (.gitignore)
```

> Why 3 weeks: a full run takes **2.5-3 days** end to end (08-28 23:38→08-31 15:53, 09-04 23:40→09-07 14:50). Weekly means the host spends nearly half its uptime rebuilding caskets whose contents barely changed. The reason for not running daily is the same as before: operator catalog digests rotate almost daily, so at any cadence the check reports "STALE" shortly after a swap.

> **3 weeks is not expressed in the timer itself.** systemd's OnCalendar has no "every 3rd week" (week numbers aren't a field, and date ranges only approximate monthly). So the timer still wakes up every Friday, and `casket-auto-update.sh --min-interval-days 21` checks `state/auto-update.last-run` to decide "is this my turn."
> - 2 out of 3 wakeups log `skipping: last completed run was Nd ago` and exit 0 (not a failure)
> - The stamp is only written on **successful completion**, so a killed run retries on the **next Friday**, not 3 weeks later
> - If the stamp is missing or corrupted, it fails open (runs). Use `--force` to run immediately
> - To change the interval, modify **`--min-interval-days` in the .service**, not the .timer. When changing, also review `CASKET_STALE_AFTER_DAYS` in `scripts/lib-freshness.sh` (default 28 days) — if the window falls below the cadence, casket-check.sh goes permanently red again

Behavior: for each phase × unit, fetches the freshness fingerprint, then:
- `current == live` → fresh (no action)
- `current == staged` → **awaiting-swap** (already built, waiting for swap. Does not rebuild — without this check, every run before swap would trigger another build)
- Otherwise → runs `casket-build.sh --apply` and registers as staged
- `a-rpm` is check-only (reports `manual-needed`, never builds automatically)

Running without arguments gives a dry-run (`would-build` display only. Dry-run is exempt from the cadence guard — you can always ask "what would be built right now"). Results are in journald (`journalctl --user -u casket-auto-update.service`) and `state/auto-update.status`. flock provides mutual exclusion across the entire run.

Manual execution (either method works):

```bash
# Via service (starts with full PATH etc., logs to journald)
systemctl --user start casket-auto-update.service
journalctl --user -u casket-auto-update.service -f      # follow progress

# Or directly (dry-run without arguments, execute with --apply)
./scripts/casket-auto-update.sh              # dry-run (shows would-build only)
./scripts/casket-auto-update.sh --apply      # execute staged build
```

Installation (one-time. Performed on casket-host on 2026-07-16):

```bash
ln -s $CASKET_WORK/systemd/casket-auto-update.service ~/.config/systemd/user/
ln -s $CASKET_WORK/systemd/casket-auto-update.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger $USER     # keep timer/jobs running even when logged out
systemctl --user enable --now casket-auto-update.timer
systemctl --user list-timers casket-auto-update.timer   # verify next fire time
```

`Persistent=true` means if the host is down at fire time, it catches up on next boot. To stop the timer: `systemctl --user disable --now casket-auto-update.timer` (manual start remains possible).

List artifacts awaiting swap with `cat state/auto-update.status` or `python3 scripts/registry.py list --status staged`.

**Phase A's swap is additive; A-rpm/B/B-operand swaps are replacements**: Phase A creates a separate mount per patch version (`/srv/sources-ocp<patch>`), so a patch bump adds a new mount while old patch mounts remain (the benefit is that old version source references keep working). `casket-swap.sh` detects this and does not retire the old `live` registry entry for Phase A (so `cleanup.sh` won't delete it). A-rpm/B/B-operand have stable mount targets (per-minor or single path), so old entries are transitioned to retired and become candidates for `cleanup.sh`.

## Upstream link-health check (upstream-link-check)

A weekly early warning for sources that could no longer be **re-fetched** from upstream (deleted, renamed or private repo; removed tag). Content that is already collected stays safe inside the caskets. Runs on casket-host since 2026-09-23. Before that it was a GitHub Actions job, but `state/` is not in the public repo, so the job never had a manifest to read.

```
systemd/upstream-link-check.service     → scripts/upstream-link-check.sh (export + check)
systemd/upstream-link-check.timer       Wakes up every Wednesday 09:00 (outside the auto-update window)
state/upstream-sources.tsv              Manifest regenerated from /srv/sources-*/…/git/INDEX.tsv on every run
state/upstream-links-baseline.txt       Accepted, known rot; only NEW rot fails the unit
```

```bash
ln -s $CASKET_WORK/systemd/upstream-link-check.service ~/.config/systemd/user/
ln -s $CASKET_WORK/systemd/upstream-link-check.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now upstream-link-check.timer
journalctl --user -u upstream-link-check.service -e    # NEW ROT / RENAMED lines
```

A failed unit means NEW rot. After reviewing it, accept it with `python3 scripts/check-upstream-links.py --write-baseline`. The GitHub token is taken the same way as for auto-update (`GITHUB_TOKEN`, else `gh auth token`). If no caskets are mounted, the export refuses to overwrite the manifest and the unit fails. It never passes on an empty manifest.

## Mount management (since 2026-07-11, fstab-less)

Casket mounts are no longer written to fstab (removed after it grew to 50+ lines; all casket lines were removed at the time of `/etc/fstab.bak-20260711-pre-reconciler`). The single sources of truth are:

```
state/registry.json live entries     ← managed by check/build/swap
config/static-mounts.tsv            ← caskets outside the registry (2 RHEL caskets,
                                       certified/community catalogs, interim srpms)
```

`scripts/casket-mounts.sh` reconciles these against actual mounts, performing mount / swap / umount as needed (dry-run by default, `--apply` requires root). At boot, `systemd/casket-mounts.service` (installed and enabled under `/etc/systemd/system/`) runs `--apply` to rebuild all mounts.

- `casket-swap.sh --apply` performs an in-place remount + marks as live in the registry. Persistence across reboots is handled by the service.
- To add a new static casket (e.g. catalog rebuild), add a line to `static-mounts.tsv` and run `sudo ./scripts/casket-mounts.sh --apply`.
- When a fresh a-rpm build lands in the registry, remove the ocp-srpms line from static-mounts.tsv to prevent dual management.
