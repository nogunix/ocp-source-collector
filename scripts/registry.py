#!/usr/bin/env python3
"""casket artifact lifecycle registry.

Tracks the *lifecycle* of built casket artifacts (staged -> live -> retired)
separately from their *content* (each artifact's own staged meta/MANIFEST.json
still describes what's inside it). One JSON file, one entry per build attempt;
the same (phase, unit) can have several entries over time as new builds are
staged, swapped live, and retired.

Storage: $CASKET_WORK/state/registry.json (override with --registry), written
atomically (tmp file + os.replace) so a crash mid-write can't corrupt it.

Subcommands: add, list, get, transition, remove. Driver scripts (casket-check.sh
/ casket-build.sh / casket-swap.sh / casket-cleanup.sh) call this instead of
editing the JSON with jq.
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_WORK = os.environ.get("CASKET_WORK") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def registry_path(args):
    if args.registry:
        return args.registry
    return os.path.join(DEFAULT_WORK, "state", "registry.json")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path):
    if not os.path.exists(path):
        return {"artifacts": []}
    with open(path) as f:
        return json.load(f)


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Preserve the existing file's owner/group/mode across the atomic
    # replace: casket-swap.sh runs this under sudo, and without this the
    # registry flips to root-owned and the unprivileged check/build/
    # auto-update runs can no longer read it.
    st = os.stat(path) if os.path.exists(path) else None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".registry-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        if st is not None:
            os.chmod(tmp, st.st_mode)
            if os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def next_entry_id(data):
    return 1 + max((e["entry_id"] for e in data["artifacts"]), default=0)


def cmd_add(args):
    path = registry_path(args)
    data = load(path)
    entry = {
        "entry_id": next_entry_id(data),
        "id": f"{args.phase}-{args.unit}",
        "phase": args.phase,
        "unit": args.unit,
        "fingerprint": args.fingerprint,
        "artifact_path": args.artifact_path,
        "mount_path": args.mount_path,
        "built_at": now_iso(),
        "status": args.status,
        "live_since": now_iso() if args.status == "live" else None,
        "retired_at": None,
    }
    data["artifacts"].append(entry)
    save(path, data)
    print(entry["entry_id"])


def _matches(e, args):
    if args.phase and e["phase"] != args.phase:
        return False
    if args.unit and e["unit"] != args.unit:
        return False
    if args.id and e["id"] != args.id:
        return False
    if args.status and e["status"] != args.status:
        return False
    return True


def cmd_list(args):
    data = load(registry_path(args))
    rows = [e for e in data["artifacts"] if _matches(e, args)]
    rows.sort(key=lambda e: e["entry_id"])
    if args.json:
        json.dump(rows, sys.stdout, indent=2, sort_keys=True)
        print()
        return
    if not rows:
        return
    cols = ["entry_id", "id", "status", "fingerprint", "built_at", "artifact_path"]
    print("\t".join(cols))
    for e in rows:
        print("\t".join(str(e.get(c, "")) for c in cols))


def cmd_get(args):
    data = load(registry_path(args))
    if args.entry_id is not None:
        matches = [e for e in data["artifacts"] if e["entry_id"] == args.entry_id]
    else:
        matches = [e for e in data["artifacts"] if _matches(e, args)]
        matches.sort(key=lambda e: e["entry_id"])
    if not matches:
        sys.exit(1)
    json.dump(matches[-1], sys.stdout, indent=2, sort_keys=True)
    print()


def cmd_transition(args):
    path = registry_path(args)
    data = load(path)
    target = next((e for e in data["artifacts"] if e["entry_id"] == args.entry_id), None)
    if target is None:
        print(f"no such entry_id: {args.entry_id}", file=sys.stderr)
        sys.exit(1)

    if args.to == "live" and args.retire_previous:
        for e in data["artifacts"]:
            if e is not target and e["id"] == target["id"] and e["status"] == "live":
                e["status"] = "retired"
                e["retired_at"] = now_iso()

    target["status"] = args.to
    if args.to == "live":
        target["live_since"] = now_iso()
    elif args.to == "retired":
        target["retired_at"] = now_iso()
    if args.mount_path:
        target["mount_path"] = args.mount_path

    save(path, data)


def cmd_remove(args):
    path = registry_path(args)
    data = load(path)
    before = len(data["artifacts"])
    data["artifacts"] = [e for e in data["artifacts"] if e["entry_id"] != args.entry_id]
    if len(data["artifacts"]) == before:
        print(f"no such entry_id: {args.entry_id}", file=sys.stderr)
        sys.exit(1)
    save(path, data)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", help="override registry.json path (default: $CASKET_WORK/state/registry.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a newly built artifact (default status: staged)")
    a.add_argument("--phase", required=True, choices=["a", "a-rpm", "b", "b-operand"])
    a.add_argument("--unit", required=True, help="minor/patch (a/b/b-operand), or 'all' (a-rpm)")
    a.add_argument("--fingerprint", required=True)
    a.add_argument("--artifact-path", required=True)
    a.add_argument("--mount-path", default=None)
    a.add_argument("--status", default="staged", choices=["staged", "live", "retired"])
    a.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="list artifacts, optionally filtered")
    ls.add_argument("--phase", choices=["a", "a-rpm", "b", "b-operand"])
    ls.add_argument("--unit")
    ls.add_argument("--id")
    ls.add_argument("--status", choices=["staged", "live", "retired"])
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    g = sub.add_parser("get", help="print the latest matching entry as JSON (or by --entry-id)")
    g.add_argument("--entry-id", type=int)
    g.add_argument("--phase", choices=["a", "a-rpm", "b", "b-operand"])
    g.add_argument("--unit")
    g.add_argument("--id")
    g.add_argument("--status", choices=["staged", "live", "retired"])
    g.set_defaults(func=cmd_get)

    t = sub.add_parser("transition", help="move one entry to a new status")
    t.add_argument("--entry-id", type=int, required=True)
    t.add_argument("--to", required=True, choices=["staged", "live", "retired"])
    t.add_argument("--mount-path", default=None)
    t.add_argument("--retire-previous", action="store_true",
                    help="when --to live: also retire any other live entry with the same id (swap semantics)")
    t.set_defaults(func=cmd_transition)

    r = sub.add_parser("remove", help="delete an entry outright (after its file is deleted by cleanup)")
    r.add_argument("--entry-id", type=int, required=True)
    r.set_defaults(func=cmd_remove)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
