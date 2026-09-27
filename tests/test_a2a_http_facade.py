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


def test_loopback_a2a_rpc_uses_the_advertised_discovery_base_url(monkeypatch):
    """POST /a2a must be served wherever the agent card advertises it.

    A loopback listener publishes ``http://127.0.0.1:<port>/a2a`` in its agent
    card without ``SONDER_A2A_BASE_URL``.  The JSON-RPC route used to read only
    the environment variable, so the advertised endpoint answered 503
    ``A2A_UNAVAILABLE``.
    """
    import http.client
    import json
    import threading
    from sonder_runtime.interfaces.http import serve

    monkeypatch.setattr(serve, "HOST", "127.0.0.1")
    monkeypatch.setattr(serve, "CONFIGURED_PORT", 11435)
    monkeypatch.delenv("SONDER_A2A_BASE_URL", raising=False)
    monkeypatch.setattr(serve, "_A2A_REQUEST_HANDLER", None)
    monkeypatch.setattr(serve, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(serve.Handler, "_auth_rate_limited", lambda self: False)
    seen = []

    def build(application, *, base_url, card_facade=None):
        seen.append(base_url)
        return lambda method, params: {"task": {"id": "task-1"}}

    monkeypatch.setattr(serve, "build_application_a2a_handler", build)
    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "GetTask",
                           "params": {"id": "task-1"}})
        conn.request("POST", "/a2a", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        status, payload = response.status, response.read()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert status == 200, payload
    assert seen == ["http://127.0.0.1:11435"]
