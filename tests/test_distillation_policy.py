from sonder_runtime.domain.distillation_policy import distillation_timeout_seconds


def test_server_production_paths_do_not_call_distillation_timeout_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_distillation_timeout_seconds"
    ]
    assert calls == []


def _env(values):
    def read(name, default):
        return values.get(name, default)

    return read


def test_uses_default_when_environment_value_is_missing():
    assert distillation_timeout_seconds(_env({}), 60) == 20


def test_clamps_to_live_server_ceiling():
    assert distillation_timeout_seconds(_env({"SONDER_DISTILLATION_TIMEOUT": 90}), 30) == 30


def test_clamps_to_one_second_lower_bound():
    assert distillation_timeout_seconds(_env({"SONDER_DISTILLATION_TIMEOUT": 0}), 30) == 1


def test_accepts_string_like_environment_values():
    assert distillation_timeout_seconds(_env({"SONDER_DISTILLATION_TIMEOUT": "7"}), 30) == 7


def test_domain_policy_does_not_read_process_environment_directly(monkeypatch):
    monkeypatch.setenv("SONDER_DISTILLATION_TIMEOUT", "99")
    assert distillation_timeout_seconds(_env({}), 30) == 20


def test_server_wrapper_preserves_live_ceiling_compatibility(monkeypatch):
    import server

    monkeypatch.setattr(
        server, "_env_int_option", lambda name, default: 45
    )
    monkeypatch.setattr(server, "TIMEOUT", 25)
    assert server._distillation_timeout_seconds() == 25
