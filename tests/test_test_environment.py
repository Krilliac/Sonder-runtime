import os


def test_pytest_harness_clears_deployment_overrides_but_keeps_timing_control(
    monkeypatch,
):
    import conftest

    monkeypatch.setenv("SONDER_CODE", "deployment-model")
    monkeypatch.setenv("SONDER_TEST_TIMINGS", "timings.jsonl")
    monkeypatch.setenv("OLLAMA_HOST", "https://deployment.example")

    conftest._clear_ambient_deployment_variables()

    assert "SONDER_CODE" not in os.environ
    assert "OLLAMA_HOST" not in os.environ
    assert os.environ["SONDER_TEST_TIMINGS"] == "timings.jsonl"


def test_pytest_harness_does_not_inherit_deployment_model_overrides():
    for name in (
        "SONDER_CODE",
        "SONDER_REASONING",
        "SONDER_OLLAMA_WORKERS",
        "SONDER_ALLOW_REMOTE_OLLAMA",
        "SONDER_EMOTION_VECTORS",
        "SONDER_SYSTEM_PROFILE",
    ):
        assert name not in os.environ

    assert os.environ["SONDER_ALLOW_CLOUD"] == "0"
