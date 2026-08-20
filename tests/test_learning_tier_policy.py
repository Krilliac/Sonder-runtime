import server

from sonder_runtime.domain.learning_tier import canonical_learn_tier


def test_local_learning_label_uses_code_gate():
    assert canonical_learn_tier("sonder") == "code"


def test_other_learning_labels_are_preserved():
    assert canonical_learn_tier("cloud-code") == "cloud-code"
    assert canonical_learn_tier("general") == "general"
    assert canonical_learn_tier(None) is None


def test_server_alias_preserves_function_identity():
    assert server._canonical_learn_tier is canonical_learn_tier
