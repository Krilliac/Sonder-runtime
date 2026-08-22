from sonder_runtime.platform.runtime_summary import local_runtime_summary


def test_server_production_paths_do_not_call_runtime_summary_compatibility_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_local_runtime_summary"
    ]
    assert calls == []


def test_local_runtime_summary_projects_all_runtime_fields():
    assert local_runtime_summary(
        {
            "num_thread": 8,
            "num_gpu": 1,
            "num_batch": 512,
            "num_ctx": 32768,
        },
        65536,
    ) == {
        "num_thread": 8,
        "num_gpu": 1,
        "num_batch": 512,
        "num_ctx_native": 32768,
        "num_ctx_requested": 65536,
    }


def test_local_runtime_summary_uses_ollama_defaults_for_missing_options():
    assert local_runtime_summary({}, 8192) == {
        "num_thread": "ollama-default",
        "num_gpu": "ollama-default",
        "num_batch": "ollama-default",
        "num_ctx_native": "ollama-default",
        "num_ctx_requested": 8192,
    }


def test_server_compatibility_delegate_preserves_runtime_summary_shape(monkeypatch):
    import server

    monkeypatch.setattr(server, "SESSION_NUM_CTX", 4096)
    monkeypatch.setattr(server.context_policy, "requested", lambda _value: 8192)
    monkeypatch.setattr(server, "_platform_local_model_options",
                        lambda *_args, **_kwargs: {"num_thread": 4, "num_ctx": 4096})
    assert server._local_runtime_summary() == {
        "num_thread": 4,
        "num_gpu": "ollama-default",
        "num_batch": "ollama-default",
        "num_ctx_native": 4096,
        "num_ctx_requested": 8192,
    }
