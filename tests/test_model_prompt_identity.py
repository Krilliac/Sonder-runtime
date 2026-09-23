from types import SimpleNamespace

import server


def test_prompt_identity_fails_closed_for_multiple_configured_workers(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server,
        "OLLAMA_POOL",
        SimpleNamespace(
            configured_origins=(
                "http://127.0.0.1:11434",
                "http://127.0.0.2:11434",
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("multi-worker identity must not probe metadata")
        ),
    )

    assert server._model_prompt_identity("model-x") == (None, None)


def test_prompt_identity_does_not_cache_positive_metadata(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server,
        "OLLAMA_POOL",
        SimpleNamespace(configured_origins=("http://127.0.0.1:11434",)),
    )
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return {
            "model_info": {"tokenizer.ggml.model": "qwen"},
            "template": "{{ .Prompt }}",
        }

    monkeypatch.setattr(server, "_post", post)
    first = server._model_prompt_identity("model-x")
    second = server._model_prompt_identity("model-x")

    assert first == second
    assert len(calls) == 2
