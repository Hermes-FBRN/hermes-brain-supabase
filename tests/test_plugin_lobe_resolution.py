import importlib.util
import os
import sys
import types
from pathlib import Path


def load_plugin(monkeypatch, tmp_path, **env):
    for key in list(os.environ):
        if key.startswith("SUPABASE_BRAIN_") or key == "OPENAI_API_KEY":
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    hermes_constants = types.ModuleType("hermes_constants")
    setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)

    agent_module = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class DummyMemoryProvider:
        pass

    setattr(memory_provider_module, "MemoryProvider", DummyMemoryProvider)
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider_module)

    tools_module = types.ModuleType("tools")
    registry_module = types.ModuleType("tools.registry")
    setattr(registry_module, "tool_error", lambda message: {"error": message})
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)

    path = Path(__file__).resolve().parents[1] / "plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("supabase_mem0_plugin_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_explicit_lobe_env_wins_over_gateway_platform_user_id(monkeypatch, tmp_path):
    plugin = load_plugin(
        monkeypatch,
        tmp_path,
        SUPABASE_BRAIN_LOBE_ID="nucleus",
        SUPABASE_BRAIN_AGENT_ID="seraph",
    )

    provider = plugin.SupabaseMem0MemoryProvider()
    provider.initialize("discord-session", user_id="462939789210157056")

    assert provider._user_id == "nucleus"
    metadata = provider._metadata(source="test")
    assert metadata["lobe_id"] == "nucleus"
    assert metadata["user_id"] == "nucleus"


def test_explicit_lobe_kwarg_still_has_highest_precedence(monkeypatch, tmp_path):
    plugin = load_plugin(monkeypatch, tmp_path, SUPABASE_BRAIN_LOBE_ID="nucleus")

    provider = plugin.SupabaseMem0MemoryProvider()
    provider.initialize("cli-session", lobe_id="project-lobe", user_id="462939789210157056")

    assert provider._user_id == "project-lobe"


def test_platform_user_id_does_not_create_lobe_without_explicit_lobe(monkeypatch, tmp_path):
    plugin = load_plugin(monkeypatch, tmp_path)

    provider = plugin.SupabaseMem0MemoryProvider()
    provider.initialize("discord-session", user_id="462939789210157056")

    assert provider._user_id == "nucleus"


def test_legacy_env_user_id_remains_explicit_fallback(monkeypatch, tmp_path):
    plugin = load_plugin(monkeypatch, tmp_path, SUPABASE_BRAIN_USER_ID="legacy-lobe")

    provider = plugin.SupabaseMem0MemoryProvider()
    provider.initialize("legacy-session", user_id="462939789210157056")

    assert provider._user_id == "legacy-lobe"
