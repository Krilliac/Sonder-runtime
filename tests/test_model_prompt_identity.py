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
    tag = {"models": [{"name": "model-x", "digest": "a" * 64, "modified_at": "rev-1"}]}
    show = {
        "model_info": {"tokenizer.ggml.model": "qwen"},
        "template": "{{ .Prompt }}",
        "modified_at": "rev-1",
    }

    def post(*_args, **_kwargs):
        calls.append(1)
        return (tag if len(calls) % 3 in (1, 0) else show)

    monkeypatch.setattr(server, "_post", post)
    first = server._model_prompt_identity("model-x")
    second = server._model_prompt_identity("model-x")

    assert first == second
    assert first[1].endswith(";ollama-model-sha256:" + "a" * 64)
    assert len(calls) == 6


def test_prompt_identity_changes_when_same_template_tag_digest_changes(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server, "OLLAMA_POOL", SimpleNamespace(configured_origins=("http://127.0.0.1:11434",))
    )
    digest = ["a" * 64, "a" * 64, "b" * 64, "b" * 64]

    def post(path, *_args, **_kwargs):
        if path == "/api/tags":
            return {"models": [{"name": "model-x", "digest": digest.pop(0), "modified_at": "rev"}]}
        return {"model_info": {"tokenizer.ggml.model": "qwen"}, "template": "same", "modified_at": "rev"}

    monkeypatch.setattr(server, "_post", post)
    first = server._model_prompt_identity("model-x")
    second = server._model_prompt_identity("model-x")
    assert first[1] != second[1]
    assert first[1].endswith("a" * 64)
    assert second[1].endswith("b" * 64)


def test_prompt_identity_fails_closed_for_missing_or_mismatched_revision(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server, "OLLAMA_POOL", SimpleNamespace(configured_origins=("http://127.0.0.1:11434",))
    )
    responses = [
        {"models": [{"name": "model-x", "modified_at": "rev"}]},
        {"models": [{"name": "model-x", "digest": "a" * 64, "modified_at": "rev"}]},
        {"model_info": {"tokenizer.ggml.model": "qwen"}, "template": "same", "modified_at": "other"},
    ]

    def post(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(server, "_post", post)
    assert server._model_prompt_identity("model-x") == (None, None)
    assert server._model_prompt_identity("model-x") == (None, None)
