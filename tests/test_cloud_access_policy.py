import server
from sonder_runtime.domain.cloud_access import cloud_allowed, cloud_disabled_message


def test_cloud_allowed_accepts_only_explicit_true_values():
    assert cloud_allowed({"SONDER_ALLOW_CLOUD": "1"})
    assert cloud_allowed({"SONDER_ALLOW_CLOUD": " YES "})
    assert not cloud_allowed({})
    assert not cloud_allowed({"SONDER_ALLOW_CLOUD": "false"})


def test_cloud_disabled_message_is_pure_and_exact():
    expected = (
        "ERROR: hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )
    assert cloud_disabled_message() == expected


def test_server_compatibility_alias_preserves_function_identity():
    assert server._cloud_disabled_message is cloud_disabled_message


def test_available_tiers_uses_packaged_cloud_policy_directly():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "available_tiers"
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cloud_allowed"
        for node in ast.walk(function)
    )


def test_serve_target_uses_packaged_cloud_policy_directly():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_serve_target"
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cloud_allowed"
        for node in ast.walk(function)
    )


def test_status_learning_and_improvement_surfaces_use_packaged_cloud_policy():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    names = {"status", "learn_tiers", "improvement_report_data"}
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
            and node.func.id == "cloud_allowed"
            for node in ast.walk(function)
        )


def test_provider_offload_and_cache_paths_use_packaged_cloud_policy():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    names = {"_post_model", "_offload_impl", "_sonder_impl"}
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
            and node.func.id == "cloud_allowed"
            for node in ast.walk(function)
        )


def test_admin_status_and_ensemble_target_paths_use_packaged_cloud_policy():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    names = {"admin_status", "_ensemble_targets"}
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
            and node.func.id == "cloud_allowed"
            for node in ast.walk(function)
        )
