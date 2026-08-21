from types import SimpleNamespace

from sonder_runtime.adapters.security.permission_policy import PermissionPolicyProvider


def test_provider_resolves_root_engine_dynamically(monkeypatch):
    import permission_modes

    provider = PermissionPolicyProvider()
    marker = SimpleNamespace(action="allow", mode="manual")
    monkeypatch.setattr(permission_modes, "decide_for_caller", lambda *a, **k: marker)
    monkeypatch.setattr(permission_modes, "MODES", ("test-mode",))
    monkeypatch.setattr(permission_modes, "MODE_LABELS", {"test-mode": "Test"})
    monkeypatch.setattr(permission_modes, "DURABLE_AUTHORITY_TOOLS", {"durable_tool"})

    assert provider.decide_for_caller(
        "x", interactive=False, gate_control_exempt=False
    ) is marker
    assert provider.modes() == ("test-mode",)
    assert provider.mode_label("test-mode") == "Test"
    assert provider.is_durable_authority_tool("/durable_tool") is True


def test_provider_forwards_mode_mutation(monkeypatch):
    import permission_modes

    calls = []
    monkeypatch.setattr(permission_modes, "set_mode", lambda name: calls.append(name) or name)
    assert PermissionPolicyProvider().set_mode("auto") == "auto"
    assert calls == ["auto"]
