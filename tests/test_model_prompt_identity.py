from types import SimpleNamespace

import pytest

import server


@pytest.fixture(autouse=True)
def clear_prompt_identity_cache():
    server._MODEL_PROMPT_IDENTITY_CACHE.clear()
    yield
    server._MODEL_PROMPT_IDENTITY_CACHE.clear()


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
        "_get",
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

    def get(*_args, **_kwargs):
        calls.append(1)
        return tag

    def post(*_args, **_kwargs):
        calls.append(1)
        return show

    monkeypatch.setattr(server, "_get", get)
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

    def get(*_args, **_kwargs):
        return {"models": [{"name": "model-x", "digest": digest.pop(0), "modified_at": "rev"}]}

    def post(*_args, **_kwargs):
        return {"model_info": {"tokenizer.ggml.model": "qwen"}, "template": "same", "modified_at": "rev"}

    monkeypatch.setattr(server, "_get", get)
    monkeypatch.setattr(server, "_post", post)
    first = server._model_prompt_identity("model-x")
    second = server._model_prompt_identity("model-x")
    assert first[1] != second[1]
    assert first[1].endswith("a" * 64)
    assert second[1].endswith("b" * 64)


def test_prompt_identity_fails_closed_for_missing_digest(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server, "OLLAMA_POOL", SimpleNamespace(configured_origins=("http://127.0.0.1:11434",))
    )
    monkeypatch.setattr(
        server, "_get",
        lambda *_args, **_kwargs: {"models": [{"name": "model-x", "modified_at": "rev"}]},
    )
    monkeypatch.setattr(
        server, "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing digest must not reach /api/show")
        ),
    )

    assert server._model_prompt_identity("model-x") == (None, None)


def test_prompt_identity_fails_closed_for_show_revision_mismatch(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server, "OLLAMA_POOL", SimpleNamespace(configured_origins=("http://127.0.0.1:11434",))
    )
    monkeypatch.setattr(
        server, "_get",
        lambda *_args, **_kwargs: {
            "models": [{"name": "model-x", "digest": "a" * 64, "modified_at": "rev"}]
        },
    )
    monkeypatch.setattr(
        server, "_post",
        lambda *_args, **_kwargs: {
            "model_info": {"tokenizer.ggml.model": "qwen"},
            "template": "same",
            "modified_at": "other",
        },
    )

    assert server._model_prompt_identity("model-x") == (None, None)


def test_prompt_identity_fails_closed_when_tag_changes_during_probe(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server, "OLLAMA_POOL", SimpleNamespace(configured_origins=("http://127.0.0.1:11434",))
    )
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(
        server, "_get",
        lambda *_args, **_kwargs: {
            "models": [{"name": "model-x", "digest": next(digests), "modified_at": "rev"}]
        },
    )
    monkeypatch.setattr(
        server, "_post",
        lambda *_args, **_kwargs: {
            "model_info": {"tokenizer.ggml.model": "qwen"},
            "template": "same",
            "modified_at": "rev",
        },
    )

    assert server._model_prompt_identity("model-x") == (None, None)


def test_prompt_identity_fails_closed_for_duplicate_tag_records(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(
        server, "OLLAMA_POOL", SimpleNamespace(configured_origins=("http://127.0.0.1:11434",))
    )
    monkeypatch.setattr(
        server, "_get",
        lambda *_args, **_kwargs: {
            "models": [
                {"name": "model-x", "digest": "a" * 64, "modified_at": "rev"},
                {"name": "model-x", "digest": "b" * 64, "modified_at": "rev"},
            ]
        },
    )
    monkeypatch.setattr(
        server, "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous tag must not reach /api/show")
        ),
    )

    assert server._model_prompt_identity("model-x") == (None, None)
