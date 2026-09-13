import os


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
