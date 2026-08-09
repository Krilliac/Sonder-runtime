"""The public repository history can shrink its privacy debt but not grow it."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_history_privacy.py"


def _module():
    spec = importlib.util.spec_from_file_location("history_privacy", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Sonder Test")
    _git(repo, "config", "user.email", "sonder-test@example.invalid")
    (repo / "README.md").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "safe")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo), "--json", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_current_history_matches_only_the_exact_baseline():
    result = _run(_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["unexpected_count"] == 0
    assert report["known_debt_count"] == 3
    assert report["clean"] is False

    release = _run(_ROOT, "--require-clean")
    assert release.returncode == 1
    assert json.loads(release.stdout)["passed"] is False


def test_clean_history_passes_release_gate(tmp_path):
    result = _run(_repo(tmp_path), "--require-clean")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["clean"] is True


def test_new_sensitive_history_fails_even_after_tip_deletion(tmp_path):
    repo = _repo(tmp_path)
    secret = repo / ".env"
    secret.write_text("SYNTHETIC_TEST_VALUE=not-a-secret\n", encoding="utf-8")
    _git(repo, "add", ".env")
    _git(repo, "commit", "-q", "-m", "add synthetic fixture")
    secret.unlink()
    _git(repo, "add", "-u")
    _git(repo, "commit", "-q", "-m", "delete synthetic fixture")

    result = _run(repo)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["unexpected_count"] == 1
    assert report["unexpected"][0]["path"] == ".env"


def test_ambient_git_redirects_cannot_hide_sensitive_history(tmp_path, monkeypatch):
    clean = _repo(tmp_path / "clean")
    dirty = _repo(tmp_path / "dirty")
    secret = dirty / "credentials.json"
    secret.write_text("synthetic fixture\n", encoding="utf-8")
    _git(dirty, "add", "credentials.json")
    _git(dirty, "commit", "-q", "-m", "synthetic sensitive path")

    monkeypatch.setenv("GIT_DIR", str(clean / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(clean))
    report = _module().inspect(dirty)
    assert report["unexpected_count"] == 1


def test_known_debt_can_only_shrink():
    module = _module()
    ids = sorted(module.KNOWN_HISTORY_PRIVACY_DEBT)
    known = [(ids[0], "combined_personal.jsonl")]
    report = module.evaluate(known)
    assert report["ok"] is True
    assert report["known_debt_count"] == 1
    assert report["removed_from_baseline_count"] == 2

    report = module.evaluate(
        known + [("0" * 40, "private-personal-lora/adapter_model.safetensors")]
    )
    assert report["ok"] is False
    assert report["unexpected_count"] == 1


def test_malformed_or_non_repository_inputs_fail_closed(tmp_path, monkeypatch):
    module = _module()
    with pytest.raises(module.HistoryPrivacyError):
        module._decode_git_path('"unterminated')

    directory = tmp_path / "plain"
    directory.mkdir()
    result = _run(directory)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["passed"] is False
    assert "Git metadata" in report["error"]

    repo = _repo(tmp_path / "nested")
    child = repo / "child"
    child.mkdir()
    with pytest.raises(module.HistoryPrivacyError, match="exact Git top level"):
        module._git_objects(child)


def test_workflows_enforce_growth_and_release_cleanliness():
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (
        _ROOT / ".github" / "workflows" / "build-apps.yml"
    ).read_text(encoding="utf-8")
    assert "python scripts/check_history_privacy.py --json" in ci
    assert "fetch-depth: 0" in ci
    assert "filter: blob:none" in ci
    assert (
        "python scripts/check_history_privacy.py --require-clean --json"
        in release
    )
