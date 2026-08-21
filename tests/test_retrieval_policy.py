from sonder_runtime.domain.retrieval_policy import no_retrieve


def test_server_production_paths_do_not_call_retrieval_compatibility_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_no_retrieve"
    ]
    assert calls == []


def test_no_retrieve_ignores_connection_and_task():
    assert no_retrieve(object(), {"prompt": "private task"}) == []


def test_no_retrieve_returns_a_fresh_empty_list():
    first = no_retrieve(None, None)
    first.append("should not leak")
    assert no_retrieve(None, None) == []


def test_server_compatibility_wrapper_uses_packaged_policy():
    import server

    assert server._no_retrieve(object(), object()) == []
