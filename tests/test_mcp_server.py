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
