"""CLI smoke tests for scripts/registry.py — verify subcommands parse
correctly, --help exits 0, and a basic add/list/get/transition/remove
round-trip works without a pre-existing registry file.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REGISTRY_PY = str(
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "registry.py")


def _run(*args, reg=None):
    cmd = [sys.executable, REGISTRY_PY]
    if reg:
        cmd += ["--registry", str(reg)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def test_help_exits_zero():
    r = _run("--help")
    assert r.returncode == 0
    assert "registry" in r.stdout.lower()


def test_subcommand_help_exits_zero():
    for sub in ("add", "list", "get", "transition", "remove"):
        r = _run(sub, "--help")
        assert r.returncode == 0, f"{sub} --help failed"


def test_add_list_get_transition_remove_roundtrip(tmp_path):
    reg = tmp_path / "registry.json"

    r = _run("add", "--phase", "a", "--unit", "4.20",
             "--fingerprint", "4.20.22", "--artifact-path", "/tmp/casket.sqfs",
             reg=reg)
    assert r.returncode == 0
    entry_id = r.stdout.strip()
    assert entry_id == "1"

    r = _run("list", "--phase", "a", "--json", reg=reg)
    assert r.returncode == 0
    items = json.loads(r.stdout)
    assert len(items) == 1
    assert items[0]["phase"] == "a"
    assert items[0]["status"] == "staged"

    r = _run("get", "--entry-id", entry_id, reg=reg)
    assert r.returncode == 0
    entry = json.loads(r.stdout)
    assert entry["fingerprint"] == "4.20.22"

    r = _run("transition", "--entry-id", entry_id, "--to", "live", reg=reg)
    assert r.returncode == 0

    r = _run("get", "--entry-id", entry_id, reg=reg)
    entry = json.loads(r.stdout)
    assert entry["status"] == "live"
    assert entry["live_since"] is not None

    r = _run("remove", "--entry-id", entry_id, reg=reg)
    assert r.returncode == 0

    r = _run("list", "--json", reg=reg)
    items = json.loads(r.stdout)
    assert items == []


def test_get_nonexistent_exits_nonzero(tmp_path):
    reg = tmp_path / "registry.json"
    r = _run("get", "--phase", "a", "--unit", "9.99", reg=reg)
    assert r.returncode != 0


def test_add_requires_mandatory_args(tmp_path):
    reg = tmp_path / "registry.json"
    r = _run("add", "--phase", "a", reg=reg)
    assert r.returncode != 0


def test_invalid_phase_rejected(tmp_path):
    reg = tmp_path / "registry.json"
    r = _run("add", "--phase", "x", "--unit", "4.20",
             "--fingerprint", "f", "--artifact-path", "/tmp/x", reg=reg)
    assert r.returncode != 0
