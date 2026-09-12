"""Tests for mcp/casket_mcp.py — verify tool registration and main()
transport selection.

Network-free: tests the wiring layer only (no live MCP/OpenGrok).
Run: pytest tests/test_mcp_server.py
"""
import importlib.util
import os
import pathlib
import sys

import pytest

MCP_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "mcp")

# casket_mcp.py imports `backends` and `mcp.server.fastmcp`. The latter is a
# third-party package that won't be installed in CI. We stub just enough of
# the import chain so the module loads and we can inspect its tool registrations.

class _FakeTool:
    """Collects @mcp.tool() calls."""
    def __init__(self):
        self.tools: dict[str, callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator

    def run(self, **kw):
        pass

    class _Settings:
        host = "0.0.0.0"
        port = 0
        transport_security = None

    settings = _Settings()


def _load_casket_mcp():
    """Load casket_mcp.py with a fake FastMCP, return (module, fake_mcp)."""
    fake = _FakeTool()

    # Stub the mcp.server.fastmcp module so the import succeeds
    import types
    stub = types.ModuleType("mcp")
    stub_server = types.ModuleType("mcp.server")
    stub_fastmcp = types.ModuleType("mcp.server.fastmcp")
    stub_fastmcp.FastMCP = lambda name: fake
    stub.server = stub_server
    stub_server.fastmcp = stub_fastmcp

    saved = {}
    for name in ("mcp", "mcp.server", "mcp.server.fastmcp"):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = {"mcp": stub, "mcp.server": stub_server,
                             "mcp.server.fastmcp": stub_fastmcp}[name]

    # Also need backends importable
    if MCP_DIR not in sys.path:
        sys.path.insert(0, MCP_DIR)

    spec = importlib.util.spec_from_file_location(
        "casket_mcp", os.path.join(MCP_DIR, "casket_mcp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig

    return mod, fake


_mod, _fake = _load_casket_mcp()

EXPECTED_TOOLS = [
    "list_versions", "list_components", "resolve_repo", "resolve_component",
    "search_text", "search_symbol", "search_refs", "grep", "read_file",
    "diff_file", "list_dir", "resolve_dependency", "permalink",
    "coverage_report",
]


def test_all_tools_registered():
    for name in EXPECTED_TOOLS:
        assert name in _fake.tools, f"tool {name!r} not registered"


def test_no_unexpected_tools():
    extra = set(_fake.tools) - set(EXPECTED_TOOLS)
    assert not extra, f"unexpected tools registered: {extra}"


def test_main_default_transport(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["casket_mcp.py"])
    assert _mod.main is not None


def test_tool_count():
    assert len(_fake.tools) == len(EXPECTED_TOOLS)


# ------------------------------------------------------------ tool forwarding
class _Recorder:
    """Stands in for the `backends` module: records the call, returns a marker."""
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def fn(*a, **kw):
            self.calls.append((name, a, kw))
            return {"_called": name}
        return fn

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(_mod, "be", r)
    return r


def _tool(name):
    return _fake.tools[name]


def test_list_versions_projects_mount_fields(monkeypatch):
    class _M:
        def __init__(self, name, phase, version, path):
            self.name, self.phase, self.version, self.path = name, phase, version, path

    mounts = [_M("sources-ocp4.20.22", "a", "4.20.22", "/srv/sources-ocp4.20.22"),
              _M("sources-ocp-srpms", "a-rpm", "", "/srv/sources-ocp-srpms")]
    monkeypatch.setattr(_mod.be, "list_mounts", lambda: mounts)

    assert _tool("list_versions")() == [
        {"mount": "sources-ocp4.20.22", "phase": "a", "version": "4.20.22",
         "path": "/srv/sources-ocp4.20.22"},
        {"mount": "sources-ocp-srpms", "phase": "a-rpm", "version": "",
         "path": "/srv/sources-ocp-srpms"},
    ]


def test_list_components_forwards_phase(rec):
    _tool("list_components")("4.18", "b")
    assert rec.last == ("list_components", ("4.18", "b"), {})


def test_list_components_empty_phase_becomes_none(rec):
    """The MCP schema needs a str default; backends wants None for "no filter"."""
    _tool("list_components")("4.18")
    assert rec.last == ("list_components", ("4.18", None), {})


def test_resolve_repo_forwards(rec):
    _tool("resolve_repo")("kubevirt", "4.20")
    assert rec.last == ("resolve_repo", ("kubevirt", "4.20"), {})


def test_resolve_component_forwards(rec):
    _tool("resolve_component")("cvo", "4.20")
    assert rec.last == ("resolve_component", ("cvo", "4.20"), {})


def test_search_text_defaults_blank_strings_to_none(rec):
    _tool("search_text")("needle")
    assert rec.last == ("search_text", ("needle", None, None, 100, True, "auto", None), {})


def test_search_text_passes_through_explicit_args(rec):
    _tool("search_text")("needle", version="4.18", path="/srv/sources-ocp4.18.46",
                         max_results=5, ignore_case=False, engine="ripgrep",
                         project="operators-4.18")
    assert rec.last == ("search_text",
                        ("needle", "4.18", "/srv/sources-ocp4.18.46", 5, False,
                         "ripgrep", "operators-4.18"), {})


def test_search_symbol_defaults(rec):
    _tool("search_symbol")("VirtualMachineInstance")
    assert rec.last == ("search_symbol", ("VirtualMachineInstance", None, None), {})


def test_search_symbol_with_project_and_path(rec):
    _tool("search_symbol")("Foo", project="ocp-4.20.27", path="/srv/x")
    assert rec.last == ("search_symbol", ("Foo", "ocp-4.20.27", "/srv/x"), {})


def test_search_refs_defaults(rec):
    _tool("search_refs")("Foo")
    assert rec.last == ("search_refs", ("Foo", None), {})


def test_search_refs_with_project(rec):
    _tool("search_refs")("Foo", "layered-4.20")
    assert rec.last == ("search_refs", ("Foo", "layered-4.20"), {})


def test_grep_defaults(rec):
    _tool("grep")("TODO", "/srv/sources-ocp4.20.22/git/cvo")
    assert rec.last == ("grep", ("TODO", "/srv/sources-ocp4.20.22/git/cvo",
                                 None, 100, False), {})


def test_grep_with_glob(rec):
    _tool("grep")("TODO", "/srv/x", glob="*.go", max_results=3, ignore_case=True)
    assert rec.last == ("grep", ("TODO", "/srv/x", "*.go", 3, True), {})


def test_read_file_zero_range_becomes_none(rec):
    """start=0/end=0 mean "whole file" — lines are 1-based, so 0 is not a range."""
    _tool("read_file")("/srv/sources-ocp4.20.22/git/cvo/main.go")
    assert rec.last == ("read_file", ("/srv/sources-ocp4.20.22/git/cvo/main.go",
                                      None, None), {})


def test_read_file_with_range(rec):
    _tool("read_file")("/srv/x", 10, 20)
    assert rec.last == ("read_file", ("/srv/x", 10, 20), {})


def test_diff_file_default_context(rec):
    _tool("diff_file")("/srv/a", "/srv/b")
    assert rec.last == ("diff_file", ("/srv/a", "/srv/b", 3), {})


def test_diff_file_custom_context(rec):
    _tool("diff_file")("/srv/a", "/srv/b", 10)
    assert rec.last == ("diff_file", ("/srv/a", "/srv/b", 10), {})


def test_list_dir_forwards(rec):
    _tool("list_dir")("/srv/sources-ocp4.20.22/git")
    assert rec.last == ("list_dir", ("/srv/sources-ocp4.20.22/git",), {})


def test_resolve_dependency_defaults(rec):
    _tool("resolve_dependency")("/srv/x", "rustls")
    assert rec.last == ("resolve_dependency", ("/srv/x", "rustls", "auto", 20), {})


def test_resolve_dependency_explicit_kind(rec):
    _tool("resolve_dependency")("/srv/x", "k8s.io/api", "go", 5)
    assert rec.last == ("resolve_dependency", ("/srv/x", "k8s.io/api", "go", 5), {})


def test_permalink_default_line(rec):
    _tool("permalink")("/srv/x/main.go")
    assert rec.last == ("permalink", ("/srv/x/main.go", 0), {})


def test_permalink_with_line(rec):
    _tool("permalink")("/srv/x/main.go", 42)
    assert rec.last == ("permalink", ("/srv/x/main.go", 42), {})


def test_coverage_report_forwards(rec):
    assert _tool("coverage_report")("4.20") == {"_called": "coverage_report"}
    assert rec.last == ("coverage_report", ("4.20",), {})


# ------------------------------------------------------------------- main()
class _RunRecorder:
    def __init__(self):
        self.transport = None

    def run(self, transport=None):
        self.transport = transport


def _run_main(monkeypatch, argv):
    """Call _mod.main() with a recording mcp object and a stubbed transport
    security module (the real `mcp` package is shadowed by this repo's mcp/)."""
    import types
    runner = _RunRecorder()

    class _Settings:
        host = None
        port = None
        transport_security = None

    runner.settings = _Settings()
    monkeypatch.setattr(_mod, "mcp", runner)
    monkeypatch.setattr(sys, "argv", argv)

    class _TSS:
        def __init__(self, **kw):
            self.kw = kw

    stub_ts = types.ModuleType("mcp.server.transport_security")
    stub_ts.TransportSecuritySettings = _TSS
    stub_server = sys.modules.get("mcp.server") or types.ModuleType("mcp.server")
    monkeypatch.setitem(sys.modules, "mcp.server", stub_server)
    monkeypatch.setitem(sys.modules, "mcp.server.transport_security", stub_ts)
    monkeypatch.setattr(stub_server, "transport_security", stub_ts, raising=False)

    _mod.main()
    return runner


def test_main_defaults_to_stdio(monkeypatch):
    runner = _run_main(monkeypatch, ["casket_mcp.py"])
    assert runner.transport == "stdio"


def test_main_unknown_mode_falls_back_to_stdio(monkeypatch):
    runner = _run_main(monkeypatch, ["casket_mcp.py", "sse"])
    assert runner.transport == "stdio"


def test_main_http_mode(monkeypatch):
    runner = _run_main(monkeypatch, ["casket_mcp.py", "http"])
    assert runner.transport == "streamable-http"
    assert runner.settings.host == "0.0.0.0"
    assert runner.settings.port == 8765


def test_main_http_disables_dns_rebinding_protection(monkeypatch):
    """LAN clients get 421 unless the default Host check is turned off."""
    runner = _run_main(monkeypatch, ["casket_mcp.py", "http"])
    assert runner.settings.transport_security.kw == {
        "enable_dns_rebinding_protection": False}
