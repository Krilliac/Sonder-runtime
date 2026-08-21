from __future__ import annotations

from types import SimpleNamespace

from sonder_runtime.adapters.model_error_formatting import (
    format_model_call_error,
    format_runtime_model_call_error,
)


def test_runtime_formatter_classifies_endpoint_target_in_the_adapter():
    error = SimpleNamespace(
        kind="other", cloud=False, attempts=1, detail="failed",
    )
    assert format_runtime_model_call_error(
        error, endpoint_loopback=True, display="local",
    ) == format_model_call_error(error, target="local Ollama", display="local")
    assert format_runtime_model_call_error(
        error, endpoint_loopback=False, display="remote",
    ) == format_model_call_error(error, target="remote Ollama", display="remote")


def test_server_core_model_paths_do_not_call_root_error_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    names = {
        "_offload_impl", "_extract_grounded_impl",
        "_sonder_impl_serialized", "_sonder_impl",
    }
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    }
    assert set(functions) == names
    for function in functions.values():
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_format_model_call_error"
            for node in ast.walk(function)
        )


def test_model_error_formatter_preserves_http_retry_hint():
    error = SimpleNamespace(
        kind="http", cloud=True, status=503, attempts=2,
        retry_after_seconds=2.4, detail="busy",
    )
    rendered = format_model_call_error(error, target="hosted Ollama", display="host")
    assert rendered.startswith("ERROR: hosted Ollama rejected the model request")
    assert "about 2s" in rendered


def test_model_error_formatter_handles_unknown_failure():
    error = SimpleNamespace(
        kind="other", cloud=False, attempts=1, detail="failed",
    )
    assert format_model_call_error(error, target="local Ollama", display="local") == (
        "ERROR contacting local Ollama at local after 1 attempt(s): failed"
    )
