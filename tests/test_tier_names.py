from sonder_runtime.domain import tier_names


def test_valid_tier_names_preserves_mapping_iteration_order():
    assert tier_names.valid_tier_names({"fast": object(), "code": object()}) == "fast, code"


def test_valid_tier_names_accepts_key_iterables():
    assert tier_names.valid_tier_names(("general", "reasoning")) == "general, reasoning"


def test_server_keeps_compatibility_delegate_for_valid_tier_names(monkeypatch):
    import server

    monkeypatch.setattr(server, "available_tiers", lambda: {"fast": object(), "code": object()})
    assert server._valid_tier_names() == "fast, code"
