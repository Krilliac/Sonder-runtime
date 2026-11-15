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


def test_live_isolated_variables_are_captured_before_the_scrub(monkeypatch):
    import conftest

    assert set(conftest._LIVE_ISOLATED_VARIABLES) == {
        "SONDER_ISOLATED_LIVE", "SONDER_ISOLATED_RUNTIME", "SONDER_ISOLATED_ROOTS",
        "SONDER_ISOLATED_IMAGE", "SONDER_ISOLATED_VOLUME_IMAGE",
    }
    # The capture must survive the scrub: a scrub that ran first would lose them.
    fake = {name: "v-" + name for name in conftest._LIVE_ISOLATED_VARIABLES}
    monkeypatch.setattr(conftest, "_CAPTURED_LIVE_ISOLATED_ENV", fake)
    for name in fake:
        monkeypatch.delenv(name, raising=False)
    conftest._clear_ambient_deployment_variables()
    assert not any(name in os.environ for name in fake)
    gen = conftest.live_isolated_environment.__wrapped__(monkeypatch)
    restored = next(gen)
    assert restored == fake
    assert all(os.environ[name] == value for name, value in fake.items())
