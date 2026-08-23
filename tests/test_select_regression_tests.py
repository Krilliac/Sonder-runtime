"""Focused contracts for the diff-driven incremental test selector."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = _REPO_ROOT / "scripts" / "select_regression_tests.py"
    spec = importlib.util.spec_from_file_location("select_regression_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Sonder Tests")
    (repo / "tests").mkdir()
    (repo / "worker.py").write_text(
        "def perform_specific_work():\n    return 1\n", encoding="utf-8"
    )
    (repo / "tests" / "test_worker.py").write_text(
        "from worker import perform_specific_work\n\n"
        "def test_work():\n    assert perform_specific_work() == 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def test_default_base_falls_back_to_local_main_without_upstream(tmp_path):
    selector = _module()
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "topic")

    assert selector.default_base(repo) == "main"


def test_changed_test_file_is_selected_without_production_identifiers(tmp_path):
    selector = _module()
    repo = _repo(tmp_path)
    test_path = repo / "tests" / "test_worker.py"
    test_path.write_text(test_path.read_text(encoding="utf-8") + "\n# regression\n")
    diff = selector.changed_diff(repo, "main")

    assert selector.parse_diff(repo, diff) == (set(), set())
    assert selector.changed_test_paths(diff) == {"tests/test_worker.py"}


def test_untracked_python_source_and_test_are_included(tmp_path):
    selector = _module()
    repo = _repo(tmp_path)
    (repo / "new_feature.py").write_text(
        "def brand_new_surface():\n    return True\n", encoding="utf-8"
    )
    (repo / "tests" / "test_new_feature.py").write_text(
        "from new_feature import brand_new_surface\n\n"
        "def test_new():\n    assert brand_new_surface()\n",
        encoding="utf-8",
    )

    diff = selector.changed_diff(repo, "HEAD")
    modules, identifiers = selector.parse_diff(repo, diff)

    assert modules == {"new_feature"}
    assert "brand_new_surface" in identifiers
    assert selector.changed_test_paths(diff) == {"tests/test_new_feature.py"}


def test_generic_entrypoint_names_do_not_expand_the_selection(tmp_path):
    selector = _module()
    source = tmp_path / "module.py"
    source.write_text(
        "def main():\n    check()\n\n"
        "def check(schema=None):\n    return perform_specific_work()\n\n"
        "def perform_specific_work():\n    return 1\n",
        encoding="utf-8",
    )
    diff = "\n".join([
        "+++ b/module.py",
        "@@ -1,0 +1,8 @@",
        "+def main():",
        "+    check()",
        "+def check():",
        "+    schema = perform_specific_work()",
        "+def perform_specific_work():",
        "+    return 1",
    ])

    _, identifiers = selector.parse_diff(tmp_path, diff)

    assert "perform_specific_work" in identifiers
    assert "main" not in identifiers
    assert "check" not in identifiers
    assert "schema" not in identifiers


def test_deleted_public_binding_remains_a_regression_term(tmp_path):
    selector = _module()
    source = tmp_path / "module.py"
    source.write_text("def replacement_surface():\n    return 2\n", encoding="utf-8")
    diff = "\n".join([
        "+++ b/module.py",
        "@@ -1,2 +1,2 @@",
        "-def removed_surface():",
        "-    return 1",
        "+def replacement_surface():",
        "+    return 2",
    ])

    _, identifiers = selector.parse_diff(tmp_path, diff)

    assert {"removed_surface", "replacement_surface"} <= identifiers


def test_json_report_is_machine_readable_and_bounded(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "topic")
    source = repo / "worker.py"
    source.write_text(
        "def perform_specific_work():\n    return 2\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "select_regression_tests.py"),
            "--repo", str(repo),
            "--since", "main",
            "--format", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert report["schema"] == "sonder.incremental-test-selection.v1"
    assert report["selected"] == ["tests/test_worker.py"]
    assert report["selected_count"] == 1
    assert report["test_file_count"] == 1
    assert report["elapsed_seconds"] >= 0
    assert str(repo) not in result.stdout
