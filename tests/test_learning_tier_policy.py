import server

from sonder_runtime.domain.learning_tier import canonical_learn_tier, should_learn


def test_local_learning_label_uses_code_gate():
    assert canonical_learn_tier("sonder") == "code"


def test_other_learning_labels_are_preserved():
    assert canonical_learn_tier("cloud-code") == "cloud-code"
    assert canonical_learn_tier("general") == "general"
    assert canonical_learn_tier(None) is None


def test_server_alias_preserves_function_identity():
    assert server._canonical_learn_tier is canonical_learn_tier


def test_should_learn_requires_opt_in_and_configured_tier():
    assert should_learn("code", True, {"code"}) is True
    assert should_learn("fast", True, {"code"}) is False
    assert should_learn("code", False, {"code"}) is False


def test_server_should_learn_uses_live_tier_configuration(monkeypatch):
    monkeypatch.setattr(server, "LEARN_TIERS", {"general"})
    assert server._should_learn("general", True) is True
    assert server._should_learn("code", True) is False


def test_should_learn_alias_is_imported_as_policy():
    assert server._should_learn_policy is should_learn
