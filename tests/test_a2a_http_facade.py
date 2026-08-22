from sonder_runtime.application.agent_registry.workbench_review import AgentRegistration
from sonder_runtime.interfaces.http.facades.a2a import A2AAgentCardFacade


def test_a2a_card_route_is_authenticated_and_renders_registered_skills():
    facade = A2AAgentCardFacade()
    route = facade.route("/.well-known/agent-card.json?format=json")
    assert route is not None
    assert route.requires_auth is True
    card = facade.card(
        [AgentRegistration("review", "reviewer", "read_only", capabilities=("inspect",))],
        base_url="https://agent.example.test",
    )
    status, body = route.render(card)
    assert status == 200
    assert body["agentCard"]["url"] == "https://agent.example.test/a2a"
    assert body["agentCard"]["skills"][0]["id"] == "review"
    assert body["digest"] == card.digest


def test_a2a_card_route_does_not_match_other_paths():
    facade = A2AAgentCardFacade()
    assert facade.route("/.well-known/agent-card") is None
    assert facade.route("/v1/admin/control-plane") is None


def test_loopback_a2a_discovery_uses_configured_listener_by_default(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve, "HOST", "127.0.0.1")
    monkeypatch.setattr(serve, "CONFIGURED_PORT", 11435)
    monkeypatch.delenv("SONDER_A2A_BASE_URL", raising=False)

    assert serve._a2a_discovery_base_url() == "http://127.0.0.1:11435"


def test_non_loopback_a2a_discovery_stays_explicit(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve, "HOST", "0.0.0.0")
    monkeypatch.delenv("SONDER_A2A_BASE_URL", raising=False)

    assert serve._a2a_discovery_base_url() == ""


def test_direct_port_override_updates_discovery_port(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve, "CONFIGURED_PORT", 11435)
    monkeypatch.delenv("SONDER_PORT", raising=False)

    assert serve._selected_listener_port(None, ["sonder", "12345"]) == 12345


def test_environment_port_updates_discovery_port(monkeypatch):
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve, "CONFIGURED_PORT", 11435)
    monkeypatch.setenv("SONDER_PORT", "12346")

    assert serve._selected_listener_port(None, ["sonder"]) == 12346
