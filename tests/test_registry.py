"""End-to-end CLI tests for scripts/registry.py (the casket artifact
lifecycle registry backing casket-check/build/swap/cleanup.sh).

Runs the actual CLI via subprocess against a tmp_path registry file --
these are integration tests for the add/get/list/transition/remove wiring,
not just the pure JSON-shuffling underneath. Network-free.
Run: pytest tests/test_registry.py
"""
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "..", "scripts", "registry.py")


def run(*args, registry):
    result = subprocess.run(
        [sys.executable, _SCRIPT, "--registry", str(registry), *args],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{args} failed: {result.stderr}"
    return result.stdout


def test_add_creates_staged_entry_and_increments_ids(tmp_path):
    reg = tmp_path / "registry.json"
    id1 = run("add", "--phase", "a", "--unit", "4.20",
              "--fingerprint", "4.20.24", "--artifact-path", "/tmp/x.sqfs.xz",
              registry=reg).strip()
    id2 = run("add", "--phase", "a", "--unit", "4.21",
              "--fingerprint", "4.21.1", "--artifact-path", "/tmp/y.sqfs.xz",
              registry=reg).strip()
    assert id1 == "1" and id2 == "2"

    data = json.loads(reg.read_text())
    assert len(data["artifacts"]) == 2
    assert data["artifacts"][0]["status"] == "staged"
    assert data["artifacts"][0]["id"] == "a-4.20"


def test_list_filters_by_phase_and_status(tmp_path):
    reg = tmp_path / "registry.json"
    run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "f1",
        "--artifact-path", "/tmp/x.sqfs.xz", registry=reg)
    run("add", "--phase", "b", "--unit", "4.20", "--fingerprint", "f2",
        "--artifact-path", "/tmp/y.sqfs.xz", registry=reg)

    out = json.loads(run("list", "--phase", "a", "--json", registry=reg))
    assert len(out) == 1 and out[0]["phase"] == "a"

    out = json.loads(run("list", "--status", "staged", "--json", registry=reg))
    assert len(out) == 2


def test_transition_to_live_retires_previous_live(tmp_path):
    reg = tmp_path / "registry.json"
    old_id = run("add", "--phase", "b", "--unit", "4.20", "--fingerprint", "old",
                  "--artifact-path", "/tmp/old.sqfs.xz", "--status", "live",
                  registry=reg).strip()
    new_id = run("add", "--phase", "b", "--unit", "4.20", "--fingerprint", "new",
                  "--artifact-path", "/tmp/new.sqfs.xz",
                  registry=reg).strip()

    run("transition", "--entry-id", new_id, "--to", "live",
        "--mount-path", "/srv/sources-ocp4.20-operators", "--retire-previous",
        registry=reg)

    data = json.loads(reg.read_text())
    by_id = {str(e["entry_id"]): e for e in data["artifacts"]}
    assert by_id[old_id]["status"] == "retired"
    assert by_id[old_id]["retired_at"] is not None
    assert by_id[new_id]["status"] == "live"
    assert by_id[new_id]["live_since"] is not None
    assert by_id[new_id]["mount_path"] == "/srv/sources-ocp4.20-operators"


def test_transition_to_live_without_retire_previous_leaves_old_live(tmp_path):
    # Phase A's swap semantics: a patch bump adds a new mount, older patches
    # stay live (see casket-swap.sh header comment).
    reg = tmp_path / "registry.json"
    old_id = run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "4.20.24",
                  "--artifact-path", "/tmp/old.sqfs.xz", "--status", "live",
                  registry=reg).strip()
    new_id = run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "4.20.25",
                  "--artifact-path", "/tmp/new.sqfs.xz",
                  registry=reg).strip()

    run("transition", "--entry-id", new_id, "--to", "live", registry=reg)

    data = json.loads(reg.read_text())
    by_id = {str(e["entry_id"]): e for e in data["artifacts"]}
    assert by_id[old_id]["status"] == "live"
    assert by_id[new_id]["status"] == "live"


def test_get_returns_latest_matching_entry(tmp_path):
    reg = tmp_path / "registry.json"
    run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "f1",
        "--artifact-path", "/tmp/x.sqfs.xz", "--status", "live", registry=reg)
    run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "f2",
        "--artifact-path", "/tmp/y.sqfs.xz", "--status", "live", registry=reg)

    out = json.loads(run("get", "--phase", "a", "--unit", "4.20", "--status", "live",
                          registry=reg))
    assert out["fingerprint"] == "f2"  # the later add, not the first


def test_accepts_a_rpm_and_b_operand_phase_values(tmp_path):
    # a-rpm and b-operand are sub-resolutions of a/b (see README.md "運用"
    # section), not independent phases -- cover the CLI choice change.
    reg = tmp_path / "registry.json"
    run("add", "--phase", "a-rpm", "--unit", "all", "--fingerprint", "f1",
        "--artifact-path", "/tmp/srpms.sqfs.xz", registry=reg)
    run("add", "--phase", "b-operand", "--unit", "4.20", "--fingerprint", "f2",
        "--artifact-path", "/tmp/layered.sqfs.xz", registry=reg)

    data = json.loads(reg.read_text())
    ids = {e["id"] for e in data["artifacts"]}
    assert ids == {"a-rpm-all", "b-operand-4.20"}


def test_get_missing_entry_exits_nonzero(tmp_path):
    reg = tmp_path / "registry.json"
    result = subprocess.run(
        [sys.executable, _SCRIPT, "--registry", str(reg),
         "get", "--phase", "a", "--unit", "9.99", "--status", "live"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_remove_deletes_entry(tmp_path):
    reg = tmp_path / "registry.json"
    entry_id = run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "f1",
                    "--artifact-path", "/tmp/x.sqfs.xz", registry=reg).strip()
    run("remove", "--entry-id", entry_id, registry=reg)
    data = json.loads(reg.read_text())
    assert data["artifacts"] == []


def test_registry_write_is_atomic_tmpfile_then_rename(tmp_path):
    # add() must never leave a stray .registry-* tmp file behind.
    reg = tmp_path / "registry.json"
    run("add", "--phase", "a", "--unit", "4.20", "--fingerprint", "f1",
        "--artifact-path", "/tmp/x.sqfs.xz", registry=reg)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".registry-")]
    assert leftovers == []
