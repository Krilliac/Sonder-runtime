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


def test_sonder_inference_provider_names_inference_not_ollama():
    from sonder_runtime.domain.runtime_identity import runtime_identity_block as block

    text = block("qwen3:14b", provider="sonder_inference")
    assert "`qwen3:14b`, an open-weights model served through Sonder Inference" in text
    assert "/v1/sonder/health" in text
    assert "Ollama" not in text and "ollama ps" not in text


def test_ollama_and_cloud_identity_unchanged_by_provider_argument():
    from sonder_runtime.domain.runtime_identity import runtime_identity_block as block

    assert block("qwen3:14b") == block("qwen3:14b", provider=None) == block("qwen3:14b", provider="ollama")
    assert "served by Ollama on this machine" in block("qwen3:14b")
    assert block("glm:cloud", cloud=True) == block("glm:cloud", cloud=True, provider="sonder_inference")
