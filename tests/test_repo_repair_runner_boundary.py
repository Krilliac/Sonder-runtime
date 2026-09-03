"""The repo-repair pytest runner lives in the adapters layer; the root name is an alias."""
import textwrap

import server
from sonder_runtime.adapters import repo_repair_runner


def _project(tmp_path, body):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "test_it.py").write_text(textwrap.dedent(body))
    return project


def test_root_helper_is_an_identity_preserving_alias():
    assert server._repo_repair_pytest is repo_repair_runner.run_pytest


def test_passing_and_failing_candidates_are_attributable(tmp_path):
    ok, output, infra = repo_repair_runner.run_pytest(_project(tmp_path, """
        def test_pass():
            assert True
    """), timeout=60)
    assert ok is True
    assert infra == ""
    assert "passed" in output
    failing = tmp_path / "failing"
    failing.mkdir()
    (failing / "test_it.py").write_text("def test_fail():\n    assert False\n")
    ok, output, infra = repo_repair_runner.run_pytest(failing, timeout=60)
    assert ok is False
    assert infra == ""
    assert "failed" in output


def test_a_timeout_is_infrastructure_not_a_verdict(tmp_path):
    ok, output, infra = repo_repair_runner.run_pytest(_project(tmp_path, """
        import time

        def test_slow():
            time.sleep(30)
    """), timeout=1)
    assert ok is False
    assert output == "pytest timed out"
    assert infra == "pytest timed out"
