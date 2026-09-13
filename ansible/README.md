# ansible

Idempotent host setup for the search infrastructure described in
[`../docs/setup.md`](../docs/setup.md) section 5: `casket-mounts.service`
(mounts every `/srv/sources-*`), `casket-mcp.service` (MCP over HTTP,
[`../mcp/README.md`](../mcp/README.md)), and `opengrok.service` (source
browser web UI, [`../opengrok/README.md`](../opengrok/README.md)).

Replaces the manual "cp the unit file, systemctl enable" steps in those
READMEs with one playbook, safe to re-run any time (host rebuild, picking up
a repo change to a `.service` file, or recovering from the SELinux/venv
failure modes below). When a unit file actually changes, the playbook
`restarts` (not just starts) that service, so an updated ExecStart takes
effect in the same run.

## Run

```bash
cd ansible
ansible-playbook -i inventory.ini casket-host.yml
```

Needs passwordless (or interactive) sudo for the system-level tasks
(SELinux relabel, `casket-mounts.service`, firewalld). Override the checkout
path if it's not `$CASKET_WORK` / `~/ocp-source-collector`:

```bash
ansible-playbook -i inventory.ini casket-host.yml -e casket_repo=/path/to/ocp-source-collector
```

## What it fixes, and why it's here

- **SELinux**: `casket-mounts.service` is a *system* unit (needs root for
  `mount(8)`) whose `ExecStart` lives under this repo's checkout in `$HOME`.
  System services run in the `init_t` domain, which is denied `execute` on
  `user_home_t` (the label everything under a user's home gets) — so the
  unit crash-loops with `status=203/EXEC` at boot, even though the exact
  same command works fine run by hand under `sudo` (interactive sudo runs
  unconfined, not `init_t`). The playbook relabels the script `bin_t`,
  which `init_t` is permitted to execute.
- **`.venv`**: `mcp/.venv` is gitignored; if it's ever wiped, `casket-mcp.service`
  crash-loops the same way pointing at a nonexistent interpreter. The
  playbook (like the service's own `ExecStartPre`) runs `mcp/ensure-venv.sh`,
  which rebuilds it from `requirements.txt` when missing.
- **Lingering**: without `loginctl enable-linger`, the user-scope
  `casket-mcp`/`opengrok` services die at logout.

## Tests

```bash
bash tests/test_ansible.sh
```

Runs in CI (`.github/workflows/ci.yml`, via the `tests/*.sh` loop). It
syntax-checks both playbooks, confirms every module they call resolves, and
runs `ansible-lint --offline` over this directory.

These are static checks only. The playbooks install systemd units, relabel a
file for SELinux and open firewalld ports, so an actual run needs a real host —
CI cannot do it, for the same reason it cannot run the collection pipeline.
`--check` against a live host is the closest thing to a dry run:

```bash
ansible-playbook -i inventory.ini casket-host.yml --check --diff
```

Read a `--check` run with the `command` tasks in mind — check mode predicts
them instead of running them, and the three here each report differently
(measured, not assumed):

| task | check-mode result | why |
| --- | --- | --- |
| `ensure-venv.sh`, `loginctl enable-linger` | `changed` if the `creates:` path is missing, `ok` if it exists | the `creates:` guard is all check mode has to go on |
| `restorecon` | `skipped` | its `changed_when` reads the command's `stdout`, and check mode produces none |

So a `changed` on those first two is a prediction, and the `skipped` restorecon
is not a problem — neither tells you the task would actually do anything.

Rule exceptions live in [`../.ansible-lint`](../.ansible-lint), one entry with
its reasoning; `tests/test_ansible.sh` fails if that list grows without the
count there being updated, so silently muting a rule is not a one-line change.

## Teardown

```bash
ansible-playbook -i inventory.ini casket-host-teardown.yml
```

Stops/disables/removes everything `casket-host.yml` installed: the two user
units, the system unit, the `bin_t` SELinux label (restored to whatever
policy defaults to), lingering, and the two firewall ports. Pass
`-e purge_venv=true` to also delete `mcp/.venv` (harmless — it self-heals on
next start).

**Does not unmount `/srv/sources-*` or touch the OpenGrok podman
container/index.** `casket-mounts.sh` itself refuses to reconcile toward an
empty desired state ("refusing to unmount everything") because this is
production infra serving live LAN users — there's no supported
unmount-everything mode, so the teardown playbook doesn't attempt one
either. To actually take sources offline, unmount by hand.

## Prerequisites

- `community.general` and `ansible.posix` collections (both ship with the
  `ansible` package on this host; `ansible-galaxy collection list` to check
  elsewhere).
- `casket-ocp-grok` podman container must already exist for the OpenGrok
  step to have something to start — create it once with
  `opengrok/scripts/run-opengrok.sh` before the first playbook run on a new
  host.
