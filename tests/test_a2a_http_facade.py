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
