"""Import-level tests for scripts/registry.py — exercises functions directly
so pytest-cov counts the lines (the subprocess CLI tests in test_registry_cli.py
don't register against --cov=scripts).

Run: pytest tests/test_registry_import.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import types

import pytest

SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")

def _load_registry():
    spec = importlib.util.spec_from_file_location(
        "registry", os.path.join(SCRIPTS_DIR, "registry.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

reg = _load_registry()


# ------------------------------------------------------------------ now_iso
def test_now_iso_format():
    ts = reg.now_iso()
    assert ts.endswith("Z")
    assert "T" in ts
    assert len(ts) == 20


# ------------------------------------------------------------------ load
def test_load_nonexistent(tmp_path):
    result = reg.load(str(tmp_path / "nope.json"))
    assert result == {"artifacts": []}


def test_load_existing(tmp_path):
    p = tmp_path / "reg.json"
    p.write_text(json.dumps({"artifacts": [{"entry_id": 1}]}))
    result = reg.load(str(p))
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["entry_id"] == 1


# ------------------------------------------------------------------ save
def test_save_creates_file(tmp_path):
    p = tmp_path / "state" / "registry.json"
    reg.save(str(p), {"artifacts": []})
    assert p.exists()
    data = json.loads(p.read_text())
    assert data == {"artifacts": []}


def test_save_preserves_permissions(tmp_path):
    p = tmp_path / "reg.json"
    p.write_text("{}")
    os.chmod(str(p), 0o600)
    reg.save(str(p), {"artifacts": [{"entry_id": 1}]})
    assert p.exists()
    mode = os.stat(str(p)).st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------- next_entry_id
def test_next_entry_id_empty():
    assert reg.next_entry_id({"artifacts": []}) == 1


def test_next_entry_id_with_entries():
    data = {"artifacts": [{"entry_id": 3}, {"entry_id": 7}, {"entry_id": 5}]}
    assert reg.next_entry_id(data) == 8


# ---------------------------------------------------------------- _matches
def _entry(**kw):
    base = {"entry_id": 1, "id": "a-4.20", "phase": "a", "unit": "4.20",
            "status": "staged", "fingerprint": "fp"}
    base.update(kw)
    return base


def _args(**kw):
    defaults = {"phase": None, "unit": None, "id": None, "status": None}
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def test_matches_no_filter():
    assert reg._matches(_entry(), _args()) is True


def test_matches_phase_hit():
    assert reg._matches(_entry(phase="a"), _args(phase="a")) is True


def test_matches_phase_miss():
    assert reg._matches(_entry(phase="b"), _args(phase="a")) is False


def test_matches_unit():
    assert reg._matches(_entry(unit="4.20"), _args(unit="4.20")) is True
    assert reg._matches(_entry(unit="4.20"), _args(unit="4.22")) is False


def test_matches_id():
    assert reg._matches(_entry(id="a-4.20"), _args(id="a-4.20")) is True
    assert reg._matches(_entry(id="a-4.20"), _args(id="b-4.20")) is False


def test_matches_status():
    assert reg._matches(_entry(status="staged"), _args(status="staged")) is True
    assert reg._matches(_entry(status="staged"), _args(status="live")) is False


def test_matches_combined():
    e = _entry(phase="a", unit="4.20", status="live")
    assert reg._matches(e, _args(phase="a", status="live")) is True
    assert reg._matches(e, _args(phase="a", status="staged")) is False


# ---------------------------------------------------------------- cmd_add
def _reg_args(tmp_path, **kw):
    defaults = {"registry": str(tmp_path / "registry.json")}
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def test_cmd_add(tmp_path, capsys):
    args = _reg_args(tmp_path, phase="a", unit="4.20", fingerprint="4.20.22",
                     artifact_path="/tmp/casket.sqfs", mount_path=None,
                     status="staged")
    reg.cmd_add(args)
    out = capsys.readouterr().out.strip()
    assert out == "1"
    data = reg.load(str(tmp_path / "registry.json"))
    assert len(data["artifacts"]) == 1
    e = data["artifacts"][0]
    assert e["phase"] == "a"
    assert e["unit"] == "4.20"
    assert e["fingerprint"] == "4.20.22"
    assert e["status"] == "staged"
    assert e["live_since"] is None


def test_cmd_add_live_status(tmp_path, capsys):
    args = _reg_args(tmp_path, phase="b", unit="4.22", fingerprint="fp",
                     artifact_path="/tmp/x.sqfs", mount_path="/srv/x",
                     status="live")
    reg.cmd_add(args)
    data = reg.load(str(tmp_path / "registry.json"))
    e = data["artifacts"][0]
    assert e["status"] == "live"
    assert e["live_since"] is not None


def test_cmd_add_increments_id(tmp_path, capsys):
    for i in range(3):
        args = _reg_args(tmp_path, phase="a", unit=f"4.{20+i}",
                         fingerprint="fp", artifact_path="/tmp/x.sqfs",
                         mount_path=None, status="staged")
        reg.cmd_add(args)
    data = reg.load(str(tmp_path / "registry.json"))
    ids = [e["entry_id"] for e in data["artifacts"]]
    assert ids == [1, 2, 3]


# ---------------------------------------------------------------- cmd_list
def _seed_registry(tmp_path):
    path = tmp_path / "registry.json"
    data = {"artifacts": [
        {"entry_id": 1, "id": "a-4.20", "phase": "a", "unit": "4.20",
         "status": "staged", "fingerprint": "fp1", "built_at": "2026-01-01T00:00:00Z",
         "artifact_path": "/tmp/a.sqfs"},
        {"entry_id": 2, "id": "b-4.22", "phase": "b", "unit": "4.22",
         "status": "live", "fingerprint": "fp2", "built_at": "2026-02-01T00:00:00Z",
         "artifact_path": "/tmp/b.sqfs"},
    ]}
    path.write_text(json.dumps(data))
    return str(path)


def test_cmd_list_json(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, phase=None, unit=None,
                                 id=None, status=None, json=True)
    reg.cmd_list(args)
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 2


def test_cmd_list_filtered(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, phase="a", unit=None,
                                 id=None, status=None, json=True)
    reg.cmd_list(args)
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["phase"] == "a"


def test_cmd_list_tsv(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, phase=None, unit=None,
                                 id=None, status=None, json=False)
    reg.cmd_list(args)
    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    assert len(lines) == 3  # header + 2 rows
    assert "entry_id" in lines[0]


def test_cmd_list_empty(tmp_path, capsys):
    rp = str(tmp_path / "empty.json")
    args = types.SimpleNamespace(registry=rp, phase=None, unit=None,
                                 id=None, status=None, json=False)
    reg.cmd_list(args)
    out = capsys.readouterr().out
    assert out == ""


# ---------------------------------------------------------------- cmd_get
def test_cmd_get_by_entry_id(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=1, phase=None,
                                 unit=None, id=None, status=None)
    reg.cmd_get(args)
    out = json.loads(capsys.readouterr().out)
    assert out["entry_id"] == 1


def test_cmd_get_by_filter(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=None, phase="b",
                                 unit=None, id=None, status=None)
    reg.cmd_get(args)
    out = json.loads(capsys.readouterr().out)
    assert out["phase"] == "b"


def test_cmd_get_not_found(tmp_path):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=999, phase=None,
                                 unit=None, id=None, status=None)
    with pytest.raises(SystemExit) as exc:
        reg.cmd_get(args)
    assert exc.value.code == 1


# ---------------------------------------------------------- cmd_transition
def test_cmd_transition_to_live(tmp_path):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=1, to="live",
                                 retire_previous=False, mount_path=None)
    reg.cmd_transition(args)
    data = reg.load(rp)
    e = [a for a in data["artifacts"] if a["entry_id"] == 1][0]
    assert e["status"] == "live"
    assert e["live_since"] is not None


def test_cmd_transition_to_retired(tmp_path):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=2, to="retired",
                                 retire_previous=False, mount_path=None)
    reg.cmd_transition(args)
    data = reg.load(rp)
    e = [a for a in data["artifacts"] if a["entry_id"] == 2][0]
    assert e["status"] == "retired"
    assert e["retired_at"] is not None


def test_cmd_transition_retire_previous(tmp_path):
    path = tmp_path / "registry.json"
    data = {"artifacts": [
        {"entry_id": 1, "id": "a-4.20", "phase": "a", "unit": "4.20",
         "status": "live", "fingerprint": "old", "built_at": "T1",
         "artifact_path": "/old", "live_since": "T1", "retired_at": None},
        {"entry_id": 2, "id": "a-4.20", "phase": "a", "unit": "4.20",
         "status": "staged", "fingerprint": "new", "built_at": "T2",
         "artifact_path": "/new", "live_since": None, "retired_at": None},
    ]}
    path.write_text(json.dumps(data))
    args = types.SimpleNamespace(registry=str(path), entry_id=2, to="live",
                                 retire_previous=True, mount_path="/srv/new")
    reg.cmd_transition(args)
    data = reg.load(str(path))
    old = [a for a in data["artifacts"] if a["entry_id"] == 1][0]
    new = [a for a in data["artifacts"] if a["entry_id"] == 2][0]
    assert old["status"] == "retired"
    assert old["retired_at"] is not None
    assert new["status"] == "live"
    assert new["mount_path"] == "/srv/new"


def test_cmd_transition_not_found(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=999, to="live",
                                 retire_previous=False, mount_path=None)
    with pytest.raises(SystemExit) as exc:
        reg.cmd_transition(args)
    assert exc.value.code == 1
    assert "no such entry_id" in capsys.readouterr().err


# ---------------------------------------------------------------- cmd_remove
def test_cmd_remove(tmp_path):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=1)
    reg.cmd_remove(args)
    data = reg.load(rp)
    assert len(data["artifacts"]) == 1
    assert data["artifacts"][0]["entry_id"] == 2


def test_cmd_remove_not_found(tmp_path, capsys):
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=999)
    with pytest.raises(SystemExit) as exc:
        reg.cmd_remove(args)
    assert exc.value.code == 1
    assert "no such entry_id" in capsys.readouterr().err


# ---------------------------------------------------------- build_parser
def test_build_parser_add():
    p = reg.build_parser()
    args = p.parse_args(["--registry", "/tmp/r.json", "add",
                         "--phase", "a", "--unit", "4.20",
                         "--fingerprint", "fp", "--artifact-path", "/tmp/x"])
    assert args.phase == "a"
    assert args.unit == "4.20"


def test_build_parser_list():
    p = reg.build_parser()
    args = p.parse_args(["list", "--phase", "b", "--json"])
    assert args.json is True


def test_build_parser_transition():
    p = reg.build_parser()
    args = p.parse_args(["transition", "--entry-id", "5", "--to", "live",
                         "--retire-previous"])
    assert args.entry_id == 5
    assert args.to == "live"
    assert args.retire_previous is True


def test_build_parser_invalid_phase():
    p = reg.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["add", "--phase", "x", "--unit", "4.20",
                       "--fingerprint", "fp", "--artifact-path", "/tmp/x"])


# ---------------------------------------------------------- registry_path
def test_registry_path_with_override():
    args = types.SimpleNamespace(registry="/custom/path.json")
    assert reg.registry_path(args) == "/custom/path.json"


def test_registry_path_default():
    args = types.SimpleNamespace(registry=None)
    result = reg.registry_path(args)
    assert result.endswith("state/registry.json")


# --------------------------------------------------------------- save() paths
def test_save_preserves_mode_of_existing_file(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{}")
    os.chmod(path, 0o640)
    reg.save(str(path), {"artifacts": []})
    assert os.stat(path).st_mode & 0o777 == 0o640
    assert json.loads(path.read_text()) == {"artifacts": []}


def test_save_new_file_gets_0644(tmp_path):
    path = tmp_path / "sub" / "registry.json"
    reg.save(str(path), {"artifacts": []})
    assert os.stat(path).st_mode & 0o777 == 0o644


def test_save_chowns_when_running_as_root(tmp_path, monkeypatch):
    """casket-swap.sh runs this under sudo; the file must keep its owner."""
    path = tmp_path / "registry.json"
    path.write_text("{}")
    monkeypatch.setattr(reg.os, "geteuid", lambda: 0)
    chowned = []
    monkeypatch.setattr(reg.os, "chown",
                        lambda p, uid, gid: chowned.append((p, uid, gid)))
    reg.save(str(path), {"artifacts": []})
    st = os.stat(path)
    assert len(chowned) == 1
    assert chowned[0][1:] == (st.st_uid, st.st_gid)


def test_save_removes_tmp_and_reraises_on_failure(tmp_path, monkeypatch):
    """A failed write must not leave a .registry-* turd next to the real file."""
    path = tmp_path / "registry.json"
    path.write_text('{"artifacts": []}')

    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(reg.json, "dump", boom)
    with pytest.raises(RuntimeError, match="disk on fire"):
        reg.save(str(path), {"artifacts": []})

    assert not [p for p in os.listdir(tmp_path) if p.startswith(".registry-")]
    assert path.read_text() == '{"artifacts": []}'   # original untouched


# ------------------------------------------------------------------- main()
def test_main_dispatches_to_subcommand(tmp_path, monkeypatch, capsys):
    path = tmp_path / "registry.json"
    monkeypatch.setattr(sys, "argv",
                        ["registry.py", "--registry", str(path),
                         "list", "--json"])
    reg.main()
    assert json.loads(capsys.readouterr().out) == []


def test_main_rejects_unknown_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["registry.py", "frobnicate"])
    with pytest.raises(SystemExit) as e:
        reg.main()
    assert e.value.code != 0


def test_cmd_transition_to_staged_sets_no_timestamp(tmp_path):
    """Only live/retired carry a timestamp; staged is the default state."""
    rp = _seed_registry(tmp_path)
    args = types.SimpleNamespace(registry=rp, entry_id=1, to="staged",
                                 mount_path=None, retire_previous=False)
    reg.cmd_transition(args)
    entry = next(e for e in reg.load(rp)["artifacts"] if e["entry_id"] == 1)
    assert entry["status"] == "staged"
    assert "live_since" not in entry
    assert "retired_at" not in entry
