import personas
from sonder_runtime.interfaces.http import serve


def test_get_explainer_returns_explainer_prompt():
    assert personas.get("explainer") == personas.PERSONAS["explainer"]


def test_get_unknown_falls_back_to_coder():
    assert personas.get("unknown") == personas.PERSONAS["coder"]


def test_get_none_falls_back_to_coder():
    assert personas.get(None) == personas.PERSONAS["coder"]


def test_get_default_is_coder():
    assert personas.get("") == personas.PERSONAS["coder"]


def test_get_case_and_whitespace_insensitive():
    assert personas.get("  Teacher  ") == personas.PERSONAS["teacher"]


def test_names_includes_all_personas():
    assert personas.names() == [
        "architect",
        "coder",
        "concise",
        "creative",
        "explainer",
        "mathematician",
        "reviewer",
        "security",
        "teacher",
    ]


def test_mathematician_keeps_conjecture_separate_from_checked_proof():
    prompt = personas.get("mathematician")
    assert "conjecture" in prompt
    assert "Lean" in prompt
    assert "numerical evidence" in prompt
    assert "constant" in prompt


def test_new_specialist_personas_render_cleanly():
    assert "defensive security" in personas.get("security").lower()
    assert "creative-writing" in personas.get("creative").lower()
    assert "systems-architect" in personas.get("architect").lower()
    assert "concise mode" in personas.get("concise").lower()


def test_normalize_selection_handles_modes_aliases_and_unknowns():
    assert personas.normalize_selection(None) == "auto"
    assert personas.normalize_selection("") == "auto"
    assert personas.normalize_selection("Auto") == "auto"
    assert personas.normalize_selection("reset") == "auto"
    assert personas.normalize_selection("none") == "off"
    assert personas.normalize_selection("OFF") == "off"
    assert personas.normalize_selection("sec") == "security"
    assert personas.normalize_selection("math") == "mathematician"
    assert personas.normalize_selection("brief") == "concise"
    assert personas.normalize_selection("arch") == "architect"
    assert personas.normalize_selection("nonexistent") is None
    assert personas.normalize_selection(123) is None


def test_resolve_auto_routes_by_tier_and_prompt_cues():
    assert personas.resolve_auto("hello", tier="security") == "security"
    assert personas.resolve_auto("hello", tier="creative") == "creative"
    assert personas.resolve_auto("hello", tier="draft") == "concise"
    assert personas.resolve_auto("Threat model this OAuth callback", tier="general") == "security"
    assert personas.resolve_auto("Please review this diff for edge cases", tier="code") == "reviewer"
    assert personas.resolve_auto("Design an architecture for event ingestion", tier="reasoning") == "architect"
    assert personas.resolve_auto("Prove that the series converges in Lean 4", tier="general") == "mathematician"
    assert personas.resolve_auto("Write me a short story about a lighthouse", tier="general") == "creative"
    assert personas.resolve_auto("Teach me closures step-by-step tutorial", tier="general") == "teacher"
    assert personas.resolve_auto("ELI5 how TLS handshakes work", tier="general") == "explainer"
    assert personas.resolve_auto("TL;DR what changed in Python 3.14?", tier="general") == "concise"
    assert personas.resolve_auto("hello", tier="general") == ""


def test_resolve_honors_pin_auto_and_off():
    assert personas.resolve("", prompt="Threat model this", tier="security") == ""
    assert personas.resolve("off", prompt="Threat model this", tier="security") == ""
    assert personas.resolve("auto", prompt="Threat model this", tier="general") == "security"
    assert personas.resolve("creative", prompt="Threat model this", tier="security") == "creative"


def test_persona_scope_binds_and_restores():
    assert personas.active_scope_persona() == ""
    with personas.persona_scope("security") as bound:
        assert bound == "security"
        assert personas.active_scope_persona() == "security"
        with personas.persona_scope("auto") as inner:
            assert inner == "auto"
            assert personas.active_scope_persona() == "auto"
        assert personas.active_scope_persona() == "security"
    assert personas.active_scope_persona() == ""


def test_http_slash_persona_shows_pins_and_resets():
    state = serve.ConversationState()
    shown = serve._handle_slash("/persona", state=state)
    assert "persona: auto" in shown
    assert "security" in shown
    assert "architect" in shown

    pinned = serve._handle_slash("/persona security", state=state)
    assert "persona: security (pinned)" in pinned
    assert state.persona == "security"

    bad = serve._handle_slash("/persona bogus", state=state)
    assert "unknown persona" in bad
    assert state.persona == "security"

    disabled = serve._handle_slash("/persona off", state=state)
    assert "persona: off" in disabled
    assert state.persona == "off"

    auto = serve._handle_slash("/persona auto", state=state)
    assert "persona: auto" in auto
    assert state.persona == "auto"


def test_http_personas_endpoint_and_chat_persona_validation(monkeypatch):
    import http.client
    import json
    from tests.test_serve_auth import _http_server

    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)

    seen_scopes = []

    def fake_answer(prompt, history=None, *args, **kwargs):
        seen_scopes.append(personas.active_scope_persona())
        return "ok"

    monkeypatch.setattr(serve._legacy_runtime(), "answer_with_history", fake_answer)

    def req(port, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    with _http_server(monkeypatch) as port:
        status, catalog = req(port, "GET", "/v1/sonder/personas")
        assert status == 200
        assert catalog["default"] == "auto"
        assert catalog["modes"] == ["auto", "off"]
        names = [entry["name"] for entry in catalog["personas"]]
        assert names == personas.names()

        status, body = req(port, "POST", "/v1/chat/completions", {
            "model": "sonder",
            "persona": "not-a-persona",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert status == 400
        assert "persona:" in body["error"]["message"]

        status, _ = req(port, "POST", "/v1/chat/completions", {
            "model": "sonder",
            "persona": "security",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert status == 200
        assert seen_scopes[-1] == "security"

        status, _ = req(port, "POST", "/v1/chat/completions", {
            "model": "sonder",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert status == 200
        assert seen_scopes[-1] == "auto"
