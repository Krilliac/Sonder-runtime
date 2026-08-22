from sonder_runtime.domain.query_limits import safe_limit


def test_server_production_paths_do_not_call_query_limit_compatibility_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_safe_limit"
    ]
    assert calls == []


def test_safe_limit_uses_default_for_invalid_values():
    assert safe_limit(None) == 10
    assert safe_limit("not-a-number", default=7) == 7


def test_safe_limit_clamps_below_one_and_above_ceiling():
    assert safe_limit(0) == 1
    assert safe_limit(-50) == 1
    assert safe_limit(101) == 100
    assert safe_limit(999, max_value=25) == 25


def test_safe_limit_accepts_integer_like_values_and_preserves_custom_bounds():
    assert safe_limit("12", default=3, max_value=20) == 12
    assert safe_limit("12", default=3, max_value=8) == 8
    assert safe_limit("bad", default=0, max_value=20) == 1


def test_server_keeps_identity_compatible_delegate():
    import server

    assert server._safe_limit(12, 3, 20) == safe_limit(12, 3, 20)
    assert server._safe_limit.__doc__
