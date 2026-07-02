import server


def test_with_footer_and_parse_roundtrip():
    out = server.with_footer("here is code", "abc123def4567890")
    assert out.endswith("[interaction_id: abc123def4567890]")
    assert server.parse_interaction_id(out) == "abc123def4567890"


def test_parse_none_when_absent():
    assert server.parse_interaction_id("just some text") is None


def test_resolve_trilobite_falls_back(monkeypatch):
    # no alias present -> code tier model
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_trilobite_model() == server.TIERS["code"]


def test_resolve_trilobite_prefers_alias(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "trilobite:latest"}]})
    assert server.resolve_trilobite_model() == "trilobite"
