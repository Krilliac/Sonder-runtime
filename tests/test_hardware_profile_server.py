from scripts import package_local_system as package
import server


def test_hardware_profile_is_direct_and_read_only_agent_tool(monkeypatch):
    calls = []

    def fake_profile_text(*, workload="general", refresh=False, model=""):
        calls.append((workload, refresh, model))
        return "portable hardware profile"

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.sonder_hardware, "profile_text", fake_profile_text)
    monkeypatch.setitem(server.TIERS, "code", "sonder:latest")

    assert server.hardware_profile("coding", True) == "portable hardware profile"
    assert server._agent_dispatch(
        "hardware_profile",
        {"workload": "research", "refresh": False},
        read_only=True,
    ) == "portable hardware profile"
    # With no explicit tag the report is sized against the bound `code` tier.
    assert calls == [
        ("coding", True, "sonder:latest"),
        ("research", False, "sonder:latest"),
    ]


def test_hardware_profile_explicit_model_overrides_the_bound_tier(monkeypatch):
    calls = []

    def fake_profile_text(*, workload="general", refresh=False, model=""):
        calls.append(model)
        return "portable hardware profile"

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.sonder_hardware, "profile_text", fake_profile_text)
    monkeypatch.setitem(server.TIERS, "code", "sonder:latest")

    server.hardware_profile("coding", False, "qwen3-coder:30b-a3b-q4_K_M")
    assert server._agent_dispatch(
        "hardware_profile",
        {"workload": "coding", "model": "qwen2.5-coder:14b"},
        read_only=True,
    ) == "portable hardware profile"
    assert calls == ["qwen3-coder:30b-a3b-q4_K_M", "qwen2.5-coder:14b"]


def test_hardware_profile_agent_tolerates_non_string_workload(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.sonder_hardware,
        "profile_text",
        lambda *, workload, refresh, model="": "ok",
    )
    assert server._agent_dispatch(
        "hardware_profile", {"workload": ["coding"]}, read_only=True,
    ) == "ok"


def test_hardware_profile_is_advertised_reloadable_and_packaged():
    assert "hardware_profile" in server.tool_manifest()
    assert "hardware_profile" in server.AGENT_TOOL_HELP
    assert "hardware_profile" in server.REPOSITORY_AGENT_TOOL_HELP
    assert "hardware_profile" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "hardware_profile" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "hardware_profile" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "sonder_hardware" in server.LIVE_RELOAD_MODULES
    assert "sonder_hardware.py" in package.REQUIRED_FILES
