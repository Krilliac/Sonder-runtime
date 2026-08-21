import server
from sonder_runtime.platform import context_selection


def test_server_production_paths_do_not_call_context_native_compatibility_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_context_native"
    ]
    assert calls == []


def test_server_context_requested_delegates_to_platform_adapter():
    assert server._context_requested is not context_selection.requested_context
    assert server._context_requested("32k") == 32000


def test_server_context_native_delegates_to_platform_adapter(monkeypatch):
    monkeypatch.setenv("SONDER_NATIVE_CONTEXT_MAX", "8k")
    assert server._context_native("32k") == 8000


def test_empty_selection_uses_server_session_default(monkeypatch):
    monkeypatch.setattr(server, "SESSION_NUM_CTX", 12345)
    assert server._context_requested("") == 12345
    assert server._context_native(None) == 12345


def test_adapter_uses_platform_default_when_no_explicit_default(monkeypatch):
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "64k")
    assert context_selection.requested_context() == 64000
