import importlib.util
import os
import sys
import types
from pathlib import Path


def load_server(monkeypatch, **env):
    for key in list(os.environ):
        if key.startswith("SUPABASE_BRAIN_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    class DummyFastMCP:
        def __init__(self, name):
            self.name = name

        def tool(self):
            def decorator(fn):
                return fn
            return decorator

    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = DummyFastMCP
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    psycopg_module = types.ModuleType("psycopg")
    psycopg_module.connect = lambda *a, **k: None
    rows_module = types.ModuleType("psycopg.rows")
    rows_module.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)

    mem0_module = types.ModuleType("mem0")
    class DummyMemory:
        pass
    mem0_module.Memory = DummyMemory
    monkeypatch.setitem(sys.modules, "mem0", mem0_module)

    path = Path(__file__).resolve().parents[1] / "mcp-server" / "server.py"
    spec = importlib.util.spec_from_file_location("brain_manager_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_admin_deployment_requires_configured_admin_token(monkeypatch):
    server = load_server(monkeypatch, SUPABASE_BRAIN_AGENT_ID="client-agent")

    assert server._is_admin_deployment() is False
    assert server._require_admin("brain_register_agent") == {
        "ok": False,
        "error": "admin_token_required",
        "operation": "brain_register_agent",
        "agent_id": "client-agent",
    }


def test_admin_deployment_enabled_by_admin_token(monkeypatch):
    server = load_server(
        monkeypatch,
        SUPABASE_BRAIN_AGENT_ID="smith-main",
        SUPABASE_BRAIN_ADMIN_TOKEN="admin-secret-token",
    )

    assert server._is_admin_deployment() is True
    assert server._require_admin("brain_register_agent") is None


def test_generate_agent_token_shape_and_hash(monkeypatch):
    server = load_server(monkeypatch, SUPABASE_BRAIN_ADMIN_TOKEN="admin-secret-token")

    token = server._generate_agent_token("client-alpha-agent")
    assert token.startswith("bat_client-alpha-agent_")
    assert len(token) > 40
    assert server._hash_agent_token(token) != token
    assert len(server._hash_agent_token(token)) == 64


def test_allowed_lobes_parser_keeps_star(monkeypatch):
    server = load_server(monkeypatch, SUPABASE_BRAIN_ALLOWED_LOBES="nucleus, client-alpha, *")

    assert server.DEFAULT_ALLOWED_LOBES == {"nucleus", "client-alpha", "*"}
    assert server._lobe_allowed("anything") is True
