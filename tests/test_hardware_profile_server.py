from scripts import package_local_system as package
import server


def test_hardware_profile_is_direct_and_read_only_agent_tool(monkeypatch):
    calls = []

    def fake_profile_text(*, workload="general", refresh=False):
        calls.append((workload, refresh))
        return "portable hardware profile"

    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.sonder_hardware, "profile_text", fake_profile_text)

    assert server.hardware_profile("coding", True) == "portable hardware profile"
    assert server._agent_dispatch(
        "hardware_profile",
        {"workload": "research", "refresh": False},
        read_only=True,
    ) == "portable hardware profile"
    assert calls == [("coding", True), ("research", False)]


def test_hardware_profile_agent_tolerates_non_string_workload(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.sonder_hardware,
        "profile_text",
        lambda *, workload, refresh: "ok",
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
