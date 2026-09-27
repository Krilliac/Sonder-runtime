"""Argument building for ``scripts/test_fast.py`` (the fast regression loop)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def test_fast():
    spec = importlib.util.spec_from_file_location(
        "sonder_test_fast_script", ROOT / "scripts" / "test_fast.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selected_run_uses_worksteal_failed_first_and_selected_files(test_fast):
    command = test_fast.pytest_command(
        "py", "auto", ["tests/test_a.py", "tests/test_b.py"], [],
    )
    assert command == [
        "py", "-m", "pytest", "-n", "auto", "--dist", "worksteal", "--ff",
        "tests/test_a.py", "tests/test_b.py",
    ]


def test_full_run_passes_no_paths_so_pytest_ini_testpaths_apply(test_fast):
    command = test_fast.pytest_command("py", "4", None, ["-q"])
    assert command == ["py", "-m", "pytest", "-n", "4", "--dist", "worksteal", "--ff", "-q"]


def test_passthrough_follows_defaults_so_it_can_override_them(test_fast):
    command = test_fast.pytest_command("py", "auto", ["tests/test_a.py"], ["-n", "2", "-x"])
    # Later occurrences win in pytest's argparse; the paths still come last.
    assert command.index("-x") > command.index("--ff")
    assert command[-1] == "tests/test_a.py"
    assert command[-4:-1] == ["-n", "2", "-x"]


def test_parse_args_splits_own_options_from_pytest_passthrough(test_fast):
    arguments, passthrough = test_fast.parse_args(
        ["--since", "abc123", "-n", "3", "-x", "--", "-k", "gate", "--all"],
    )
    assert arguments.since == "abc123"
    assert arguments.workers == "3"
    assert arguments.all is False  # after --, --all belongs to pytest
    assert passthrough == ["-x", "-k", "gate", "--all"]


def test_scope_options_are_mutually_exclusive(test_fast):
    with pytest.raises(SystemExit):
        test_fast.parse_args(["--all", "--since", "HEAD~1"])
    with pytest.raises(SystemExit):
        test_fast.parse_args(["--working-tree", "--all"])


def test_since_resolution_prefers_explicit_scope_then_merge_base(test_fast, monkeypatch):
    seen = []
    monkeypatch.setattr(test_fast, "merge_base", lambda repo: seen.append(repo) or "base-sha")
    arguments, _ = test_fast.parse_args(["--working-tree"])
    assert test_fast.resolve_since(arguments, ROOT) == "HEAD"
    arguments, _ = test_fast.parse_args(["--since", "v1.0"])
    assert test_fast.resolve_since(arguments, ROOT) == "v1.0"
    assert seen == []
    arguments, _ = test_fast.parse_args([])
    assert test_fast.resolve_since(arguments, ROOT) == "base-sha"
    assert seen == [ROOT]


def test_selector_command_asks_for_json_and_omits_since_when_unknown(test_fast):
    command = test_fast.selector_command("py", None)
    assert command[:2] == ["py", str(test_fast.SELECTOR)]
    assert command[command.index("--format") + 1] == "json"
    assert "--since" not in command
    command = test_fast.selector_command("py", "base-sha")
    assert command[command.index("--since") + 1] == "base-sha"


def test_uncovered_report_names_every_uncovered_identifier(test_fast):
    report = test_fast.uncovered_report({
        "selected_count": 3,
        "test_file_count": 900,
        "uncovered_identifiers": ["_alpha", "beta_gamma"],
        "fallback_to_module": True,
    })
    assert "selected 3 of 900" in report
    assert "FALLBACK" in report
    assert "2 changed identifier(s) NO test mentions" in report
    assert "#   _alpha" in report and "#   beta_gamma" in report
    clean = test_fast.uncovered_report({"selected_count": 1, "test_file_count": 2})
    assert "uncovered identifiers: none" in clean


def test_vacuous_selection_is_reported_not_run(test_fast, monkeypatch):
    calls = []
    monkeypatch.setattr(test_fast, "select", lambda python, since: (2, None))
    monkeypatch.setattr(test_fast.subprocess, "call", lambda *a, **k: calls.append(a) or 0)
    assert test_fast.main(["--since", "HEAD"]) == 2
    assert calls == []


def test_dry_run_prints_the_selected_command(test_fast, monkeypatch, capsys):
    monkeypatch.setattr(test_fast, "select", lambda python, since: (0, {
        "selected": ["tests/test_a.py"], "selected_count": 1, "test_file_count": 5,
        "uncovered_identifiers": ["_lonely"],
    }))
    monkeypatch.setattr(test_fast.subprocess, "call", lambda *a, **k: pytest.fail("ran"))
    assert test_fast.main(["--since", "HEAD", "--dry-run", "--", "-q"]) == 0
    captured = capsys.readouterr()
    assert "--dist worksteal --ff -q tests/test_a.py" in captured.out
    assert "_lonely" in captured.err
    assert sys.executable in captured.out
