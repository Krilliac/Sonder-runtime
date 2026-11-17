import logging

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


def test_policy_error_never_logs_inline_credentials(caplog):
    name = "sonder_runtime.domain.ollama_policy"
    with caplog.at_level(logging.DEBUG, logger=name):
        assert ollama_policy.policy_error("https://user:secretpw@127.0.0.1:11434")
        assert ollama_policy.policy_error("http://127.0.0.1:11434") == ""
    assert [r for r in caplog.records if r.name == name]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in text
    assert "user:" not in text


def test_policy_error_logs_and_messages_never_carry_the_origin(caplog, monkeypatch):
    monkeypatch.delenv(ollama_policy.REMOTE_OPT_IN, raising=False)
    name = "sonder_runtime.domain.ollama_policy"
    values = [
        ("https://user:secretpw@models.test:443", True),
        ("http://user:secretpw@models.test:11434", True),
        ("https://models.test:443", False),
        ("https://models.test:443", True),
        ("http://models.test:11434", True),
        ("http://127.0.0.1:11434", False),
    ]
    with caplog.at_level(logging.DEBUG, logger=name):
        results = [ollama_policy.policy_error(v, allow_remote=r) for v, r in values]
    text = "\n".join([r.getMessage() for r in caplog.records] + results)
    assert caplog.records
    for needle in ("secretpw", "user:", "models.test", "127.0.0.1"):
        assert needle not in text


def test_remote_acceptance_warning_is_logged_once_per_origin(caplog, monkeypatch):
    monkeypatch.delenv(ollama_policy.REMOTE_OPT_IN, raising=False)
    with ollama_policy._LOG_LOCK:
        ollama_policy._LOGGED_ORIGINS.clear()
    name = "sonder_runtime.domain.ollama_policy"
    with caplog.at_level(logging.WARNING, logger=name):
        assert ollama_policy.policy_error("https://probe.example:8443", allow_remote=True) == ""
        assert ollama_policy.policy_error("https://probe.example:8443", allow_remote=True) == ""
        assert ollama_policy.policy_error("https://probe.example:8443", allow_remote=True) == ""
    warnings = [r for r in caplog.records if "remote Ollama origin accepted" in r.getMessage()]
    assert len(warnings) == 1
