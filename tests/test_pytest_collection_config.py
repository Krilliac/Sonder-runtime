import ast
from pathlib import Path


def test_bare_pytest_collects_shipped_proposal_tests():
    # If proposals disappears from testpaths, shipped compatibility tests can
    # break while the repository's only CI invocation remains green.
    config = (Path(__file__).resolve().parents[1] / "pytest.ini").read_text(
        encoding="utf-8"
    )
    testpaths = next(
        line.split("=", 1)[1].split()
        for line in config.splitlines()
        if line.strip().startswith("testpaths")
    )
    assert {"tests", "proposals"} <= set(testpaths)


def test_live_provider_credentials_are_only_restored_by_explicit_tests():
    repo_root = Path(__file__).resolve().parents[1]
    suite_fixtures = ast.parse(
        (repo_root / "tests" / "conftest.py").read_text(encoding="utf-8")
    )
    legacy_boundary = next(
        node
        for node in suite_fixtures.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_configure_http_legacy_boundary"
    )
    assert "live_provider_environment" not in {
        argument.arg for argument in legacy_boundary.args.args
    }

    live_smoke = ast.parse(
        (repo_root / "tests" / "live" / "test_model_gateway_live_smoke.py").read_text(
            encoding="utf-8"
        )
    )
    smoke_test = next(
        node
        for node in live_smoke.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_live_model_gateway_smoke"
    )
    assert "live_provider_environment" in {
        argument.arg for argument in smoke_test.args.args
    }
