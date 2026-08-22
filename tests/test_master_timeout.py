import server

from sonder_runtime.domain.master_timeout import master_timeout


def test_server_production_paths_do_not_call_master_timeout_compatibility_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_master_timeout"
    ]
    assert calls == []


def test_server_retains_master_timeout_compatibility_delegate():
    assert server._master_timeout_policy is master_timeout


def test_master_timeout_defaults_and_clamps_to_runtime_bounds():
    assert master_timeout("120", 150, 15, 300) == 120
    assert master_timeout("5", 150, 15, 300) == 15
    assert master_timeout("900", 150, 15, 300) == 300


def test_master_timeout_uses_safe_maximum_for_invalid_values():
    assert master_timeout(None, 90, 15, 120) == 90
    assert master_timeout("not-a-number", 90, 15, 120) == 90
    assert master_timeout(object(), 90, 15, 120) == 90


def test_root_master_timeout_reads_named_environment_option(monkeypatch):
    monkeypatch.setattr(server, "TIMEOUT", 80)
    monkeypatch.setenv("SONDER_TEST_MASTER_TIMEOUT", "7")
    assert server._master_timeout("SONDER_TEST_MASTER_TIMEOUT", 45) == 15
    monkeypatch.setenv("SONDER_TEST_MASTER_TIMEOUT", "60")
    assert server._master_timeout("SONDER_TEST_MASTER_TIMEOUT", 45) == 60
    monkeypatch.delenv("SONDER_TEST_MASTER_TIMEOUT")
    assert server._master_timeout("SONDER_TEST_MASTER_TIMEOUT", 45) == 45
