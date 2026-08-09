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
    assert report["known_debt_count"] == 7
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
    known = [sorted(module.KNOWN_HISTORY_PRIVACY_DEBT)[0]]
    report = module.evaluate(known)
    assert report["ok"] is True
    assert report["known_debt_count"] == 1
    assert report["removed_from_baseline_count"] == 6

    known_id, known_path = known[0]
    report = module.evaluate(
        known + [(known_id, "renamed/" + known_path)]
    )
    assert report["ok"] is False
    assert report["unexpected_count"] == 1


def test_duplicate_object_at_another_sensitive_path_is_growth():
    module = _module()
    known_id, known_path = sorted(module.KNOWN_HISTORY_PRIVACY_DEBT)[0]
    report = module.evaluate([
        (known_id, known_path),
        (known_id, "duplicate/" + known_path),
    ])

    assert report["known_debt_count"] == 1
    assert report["unexpected_count"] == 1
    assert report["unexpected"][0]["path"] == "duplicate/" + known_path

    substitution = module.evaluate([("0" * 40, known_path)])
    assert substitution["known_debt_count"] == 0
    assert substitution["unexpected_count"] == 1


def test_shallow_history_fails_closed(tmp_path):
    source = _repo(tmp_path / "source")
    (source / "second.txt").write_text("second\n", encoding="utf-8")
    _git(source, "add", "second.txt")
    _git(source, "commit", "-q", "-m", "second")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", source.as_uri(), str(shallow)],
        check=True,
        capture_output=True,
    )

    result = _run(shallow, "--require-clean")

    assert result.returncode == 1
    assert "complete Git history is required" in json.loads(result.stdout)["error"]


def test_blobless_partial_clone_remains_inspectable(tmp_path):
    source = _repo(tmp_path / "source")
    _git(source, "config", "uploadpack.allowFilter", "true")
    partial = tmp_path / "partial"
    subprocess.run(
        [
            "git", "clone", "-q", "--filter=blob:none", "--no-checkout",
            source.as_uri(), str(partial),
        ],
        check=True,
        capture_output=True,
    )

    result = _run(partial, "--require-clean")

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["clean"] is True


def test_replace_ref_cannot_hide_sensitive_commit(tmp_path):
    repo = _repo(tmp_path)
    safe_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    (repo / ".env").write_text("synthetic fixture\n", encoding="utf-8")
    _git(repo, "add", ".env")
    _git(repo, "commit", "-q", "-m", "sensitive")
    dirty_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    _git(repo, "replace", dirty_commit, safe_commit)

    report = _module().inspect(repo)

    assert report["unexpected_count"] == 1


def test_legacy_graft_cannot_hide_history(tmp_path):
    module = _module()
    repo = _repo(tmp_path)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    grafts = repo / ".git" / "info" / "grafts"
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_text(head + "\n", encoding="ascii")

    with pytest.raises(module.HistoryPrivacyError, match="grafts"):
        module.inspect(repo)


def test_inventory_output_is_bounded(tmp_path, monkeypatch):
    module = _module()
    repo = _repo(tmp_path)
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 1)

    with pytest.raises(module.HistoryPrivacyError, match="safety limit"):
        module.inspect(repo)


def test_git_inventory_timeout_fails_closed(tmp_path, monkeypatch):
    module = _module()
    repo = _repo(tmp_path)

    class HungGit:
        returncode = None

        def poll(self):
            return None

        def kill(self):
            self.returncode = -9

        def wait(self):
            return self.returncode

    ticks = iter((0.0, 100.0))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_kw: HungGit())
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(module.HistoryPrivacyError, match="timed out"):
        module.inspect(repo)


def test_text_diagnostic_escapes_control_paths(monkeypatch, capsys):
    module = _module()
    control_path = "private\x1b[31m/.env"
    monkeypatch.setattr(module, "inspect", lambda _repo: {
        "schema": 1,
        "ok": False,
        "clean": False,
        "known_debt_count": 0,
        "unexpected": [{"object_id": "0" * 12, "path": control_path}],
    })

    assert module.main(["--repo", str(_ROOT)]) == 1
    error = capsys.readouterr().err
    assert "\x1b" not in error
    assert "\\u001b" in error


def test_nul_delimited_inventory_preserves_quoted_and_control_paths():
    module = _module()
    object_id = b"1" * 40
    path = 'quoted"dir\ncontrol\x1b/.env'
    raw = (
        b":000000 100644 " + b"0" * 40 + b" " + object_id + b" A\0"
        + path.encode("utf-8") + b"\0"
    )

    objects = module._parse_raw_changes(raw)
    report = module.evaluate(objects)

    assert objects == [(object_id.decode("ascii"), path)]
    assert report["unexpected"] == [
        {"object_id": "1" * 12, "path": path}
    ]


def test_malformed_or_non_repository_inputs_fail_closed(tmp_path):
    module = _module()
    with pytest.raises(module.HistoryPrivacyError):
        module._parse_raw_changes(b"malformed\0path\0")

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
