from sonder_runtime.domain import ollama_policy


def test_domain_policy_is_transport_independent_and_fail_closed(monkeypatch):
    monkeypatch.delenv(ollama_policy.REMOTE_OPT_IN, raising=False)
    assert ollama_policy.policy_error("127.0.0.1:11434") == ""
    assert "non-loopback" in ollama_policy.policy_error("https://models.test:443")
    assert ollama_policy.policy_error(
        "https://models.test:443", allow_remote=False
    )


def test_domain_policy_matches_endpoint_adapter_surface():
    from sonder_runtime.adapters.inference import ollama_endpoint as endpoint

    values = [
        "127.0.0.1:11434", "0.0.0.0:11434", "http://[::1]:11434",
        "https://models.test:443", "http://models.test:11434/path",
    ]
    for value in values:
        assert endpoint.normalize(value) == ollama_policy.normalize(value)
        assert endpoint.is_loopback(value) == ollama_policy.is_loopback(value)
        assert endpoint.policy_error(value, allow_remote=False) == (
            ollama_policy.policy_error(value, allow_remote=False)
        )
