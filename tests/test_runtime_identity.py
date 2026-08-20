from sonder_runtime.domain.runtime_identity import runtime_identity_block


def test_identity_names_only_the_resolved_model():
    block = runtime_identity_block("sonder:latest")

    assert "sonder:latest" in block
    assert "GPT-4" in block
    assert "do not know" in block.lower()


def test_identity_omits_unknown_model_instead_of_guessing():
    assert runtime_identity_block("") == ""
    assert runtime_identity_block(None) == ""


def test_hosted_identity_does_not_claim_local_execution():
    block = runtime_identity_block("kimi-k2.7-code:cloud", cloud=True)

    assert "not on this machine" in block
    assert "served by Ollama on this machine" not in block
